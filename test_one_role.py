"""Single-champ role-weight scrape probe."""
import asyncio, sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent / "server"))
from playwright.async_api import async_playwright
import scraper


async def main():
    pw = await async_playwright().start()
    await scraper.init(pw)
    try:
        url = "https://lolalytics.com/lol/aatrox/build/"
        ctx = await scraper._new_context()
        page = await ctx.new_page()
        page.set_default_timeout(60000)
        await page.goto(url, wait_until="domcontentloaded", timeout=30000)
        await scraper._settle(page, timeout_ms=10000)
        print(f"[probe] final URL: {page.url}")
        body_len = await page.evaluate("document.body.innerText.length")
        print(f"[probe] body text length: {body_len}")
        raw = await scraper._safe_evaluate(page, scraper.CHAMP_ROLE_JS)
        print(f"[probe] raw: {raw!r}")
        title = await page.title()
        print(f"[probe] title: {title}")
        sample = await page.evaluate("document.body.innerText.slice(0, 600)")
        print(f"[probe] body sample:\n{sample}\n---")
        await ctx.close()
    finally:
        await scraper.shutdown()
        await pw.stop()


if __name__ == "__main__":
    asyncio.run(main())
