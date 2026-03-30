import asyncio, json
from playwright.async_api import async_playwright

async def debug():
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        ctx = await browser.new_context(
            user_agent='Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36')
        page = await ctx.new_page()

        try:
            await page.goto('https://u.gg/lol/champions/azir/build?role=mid',
                            wait_until='domcontentloaded', timeout=30000)
            # Simulate EXTRACT_JS: wait for .perk-active (up to 15s) + 1s extra
            await page.wait_for_selector('.perk-active', timeout=15000)
            await page.wait_for_timeout(2000)
            print("Runes loaded, now checking items...")
        except Exception as e:
            print(f"error: {e}")
            await page.wait_for_timeout(5000)

        result = await page.evaluate("""async () => {
            const spriteEls = [...document.querySelectorAll('[style*="sprite/item"]')];
            let spriteLookup = {};
            for (const el of spriteEls) {
                const bgImg = el.style.backgroundImage || '';
                const vm = bgImg.match(/riot_static\\/([\\.\\d]+)\\/img\\/sprite\\/item/);
                if (!vm) continue;
                try {
                    const itemUrl = `https://static.bigbrain.gg/assets/lol/riot_static/${vm[1]}/data/en_US/item.json`;
                    const resp = await fetch(itemUrl);
                    const itemData = await resp.json();
                    for (const [id, item] of Object.entries(itemData.data || {})) {
                        if (!item.image) continue;
                        const sprite = item.image.sprite.replace(/\\.(png|webp)$/, '');
                        spriteLookup[`${sprite}|${item.image.x}|${item.image.y}`] = parseInt(id);
                    }
                } catch(e) {}
                break;
            }

            const findSectionContainer = (keyword) => {
                const allEls = [...document.querySelectorAll('*')];
                for (const el of allEls) {
                    if (el.children.length > 0) continue;
                    const txt = el.textContent.trim().toLowerCase();
                    if (!txt.startsWith(keyword) || txt.length > keyword.length + 80) continue;
                    let cur = el.parentElement;
                    for (let d = 0; d < 12; d++) {
                        if (!cur || cur === document.body) break;
                        const sprites = cur.querySelectorAll('[style*="sprite/item"]');
                        if (sprites.length >= 2) return {container: cur, depth: d, spriteCount: sprites.length, text: cur.textContent.slice(0,100)};
                        cur = cur.parentElement;
                    }
                }
                return null;
            };

            const extractIds = (container) => {
                if (!container) return [];
                const found = [];
                const els = container.querySelectorAll('[style*="sprite/item"]');
                for (const el of els) {
                    const bgImg = el.style.backgroundImage || '';
                    const bgPos = el.style.backgroundPosition || '';
                    const sm = bgImg.match(/sprite\\/(item\\d+)\\.(webp|png)/);
                    const pm = bgPos.match(/(-?\\d+)px\\s+(-?\\d+)px/);
                    if (!sm || !pm) continue;
                    const itemId = spriteLookup[`${sm[1]}|${Math.abs(parseInt(pm[1]))}|${Math.abs(parseInt(pm[2]))}`];
                    if (itemId && !found.includes(itemId)) found.push(itemId);
                }
                return found;
            };

            const startInfo = findSectionContainer('starting items');
            const coreInfo  = findSectionContainer('core items');

            return {
                startInfo: startInfo ? {depth: startInfo.depth, spriteCount: startInfo.spriteCount, text: startInfo.text} : null,
                coreInfo: coreInfo ? {depth: coreInfo.depth, spriteCount: coreInfo.spriteCount, text: coreInfo.text} : null,
                startIds: startInfo ? extractIds(startInfo.container) : [],
                coreIds: coreInfo ? extractIds(coreInfo.container) : [],
                totalSprites: document.querySelectorAll('[style*="sprite/item"]').length
            };
        }""")

        print(json.dumps(result, indent=2))
        await browser.close()

asyncio.run(debug())
