"""
RuneSync scraping server — FastAPI + Playwright headless Chromium.

Endpoints:
  GET /build?champion=Darius&role=top&rank=Platinum%2B&region=World
  GET /counters?champion=Darius&role=top&top_n=5
  GET /matchup?my_champ=Darius&enemy_champ=Garen&role=top
  GET /role-weights
  GET /patch

Results are cached by patch version. A new patch automatically invalidates
old cache entries (different key), so no manual flushing is needed.
"""

import asyncio, json, ssl, urllib.request
from contextlib import asynccontextmanager
from typing import Optional

from fastapi import FastAPI, HTTPException, Query
from playwright.async_api import async_playwright

import cache
import scraper

_MISS = cache.MISS  # "no data found" sentinel — distinct from None ("not cached")

# ── patch version (refreshed every 6 hours) ───────────────────────────────

_patch_cache = {"value": "unknown", "fetched_at": 0.0}
_patch_lock = asyncio.Lock()


async def _get_patch() -> str:
    import time
    async with _patch_lock:
        if time.time() - _patch_cache["fetched_at"] < 6 * 3600:
            return _patch_cache["value"]
        try:
            loop = asyncio.get_event_loop()
            ssl_ctx = ssl.create_default_context()
            data = await loop.run_in_executor(
                None,
                lambda: json.loads(
                    urllib.request.urlopen(
                        "https://ddragon.leagueoflegends.com/api/versions.json",
                        context=ssl_ctx, timeout=8
                    ).read()
                )
            )
            new_patch = data[0]
            old_patch = _patch_cache["value"]
            if old_patch not in ("unknown", new_patch):
                n = cache.purge_patch(old_patch)
                print(f"[server] patch {old_patch} → {new_patch}: purged {n} stale cache entries", flush=True)
            _patch_cache["value"] = new_patch
            _patch_cache["fetched_at"] = time.time()
        except Exception as e:
            print(f"[server] patch fetch failed: {e}", flush=True)
        return _patch_cache["value"]


# ── per-key scrape locks (prevent duplicate in-flight scrapes) ─────────────

_scrape_locks: dict[str, asyncio.Lock] = {}
_locks_mutex = asyncio.Lock()


async def _get_lock(key: str) -> asyncio.Lock:
    async with _locks_mutex:
        if key not in _scrape_locks:
            _scrape_locks[key] = asyncio.Lock()
        return _scrape_locks[key]


# ── lifespan (browser startup/shutdown) ───────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI):
    print("[server] Starting Playwright browser...", flush=True)
    pw = await async_playwright().start()
    await scraper.init(pw)
    print("[server] Ready.", flush=True)
    yield
    print("[server] Shutting down browser...", flush=True)
    await scraper.shutdown()
    await pw.stop()


app = FastAPI(title="RuneSync Scraping Server", lifespan=lifespan)


# ── endpoints ─────────────────────────────────────────────────────────────

@app.get("/patch")
async def get_patch():
    return {"patch": await _get_patch()}


@app.get("/build")
async def get_build(
    champion: str = Query(...),
    role: str = Query("auto"),
    rank: str = Query("Platinum+"),
    region: str = Query("World"),
):
    patch = await _get_patch()
    # rank/region not currently used in URL (same as original ugg_api.py)
    key = f"build_v3_{champion.lower()}_{role.lower()}_{patch}"

    cached = cache.get(key)
    if cached is not None:
        return cached

    lock = await _get_lock(key)
    async with lock:
        # Re-check after acquiring lock (another request may have scraped it)
        cached = cache.get(key)
        if cached is not None:
            return cached

        try:
            result = await scraper.scrape_build(champion, role)
        except Exception as e:
            print(f"[server] build scrape failed ({champion}/{role}): {e}", flush=True)
            raise HTTPException(status_code=503, detail=str(e))

        if result is None:
            raise HTTPException(status_code=404, detail=f"No build data for {champion}/{role}")

        cache.set(key, result)
        return result


@app.get("/counters")
async def get_counters(
    champion: str = Query(...),
    role: str = Query("auto"),
    top_n: int = Query(5),
):
    patch = await _get_patch()
    key = f"counters_{champion.lower()}_{role.lower()}_{patch}"

    cached = cache.get(key)
    if cached is not None:
        return cached

    lock = await _get_lock(key)
    async with lock:
        cached = cache.get(key)
        if cached is not None:
            return cached

        try:
            result = await scraper.scrape_counters(champion, role, top_n)
        except Exception as e:
            print(f"[server] counters scrape failed ({champion}/{role}): {e}", flush=True)
            raise HTTPException(status_code=503, detail=str(e))

        cache.set(key, result)
        return result


@app.get("/matchup")
async def get_matchup(
    my_champ: str = Query(...),
    enemy_champ: str = Query(...),
    role: str = Query("auto"),
):
    patch = await _get_patch()
    key = f"matchup_{my_champ.lower()}_{enemy_champ.lower()}_{role.lower()}_{patch}"

    cached = cache.get(key)
    if cached is not None:
        if cached == _MISS:
            raise HTTPException(status_code=404, detail=f"No matchup data for {my_champ} vs {enemy_champ}")
        return cached

    lock = await _get_lock(key)
    async with lock:
        cached = cache.get(key)
        if cached is not None:
            if cached == _MISS:
                raise HTTPException(status_code=404, detail=f"No matchup data for {my_champ} vs {enemy_champ}")
            return cached

        try:
            result = await scraper.scrape_matchup(my_champ, enemy_champ, role)
        except Exception as e:
            print(f"[server] matchup scrape failed ({my_champ} vs {enemy_champ}): {e}", flush=True)
            raise HTTPException(status_code=503, detail=str(e))

        if result is None:
            cache.set(key, _MISS)
            raise HTTPException(status_code=404, detail=f"No matchup data for {my_champ} vs {enemy_champ}")

        cache.set(key, result)
        return result


@app.get("/role-weights")
async def get_role_weights():
    patch = await _get_patch()
    key = f"role_weights_{patch}"

    cached = cache.get(key)
    if cached is not None:
        return cached

    lock = await _get_lock(key)
    async with lock:
        cached = cache.get(key)
        if cached is not None:
            return cached

        try:
            result = await scraper.scrape_role_weights()
        except Exception as e:
            print(f"[server] role weights scrape failed: {e}", flush=True)
            raise HTTPException(status_code=503, detail=str(e))

        cache.set(key, result)
        return result


@app.get("/health")
async def health():
    return {"status": "ok"}


# ── dev entry point ────────────────────────────────────────────────────────

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=False)
