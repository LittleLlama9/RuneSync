"""
scraper.py — Playwright-based scraper for u.gg and LoLalytics.

Port of ugg_api.py and role_updater.py from CDP/Brave to headless Chromium
via Playwright. The JS extraction strings are reused verbatim.

Usage:
    await scraper.init(playwright_instance)
    build = await scraper.scrape_build("Darius", "top")
    await scraper.shutdown()
"""

import asyncio, json, re, ssl, urllib.request
from typing import Optional

# ── constants (same as ugg_api.py) ────────────────────────────────────────

ROLE_MAP = {
    "jungle": "jungle", "support": "support",
    "bot": "adc", "adc": "adc", "top": "top", "mid": "mid",
}

SHARD_IDS = {
    "statmodsadaptiveforceicon":  5008,
    "statmodsattackspeedicon":    5005,
    "statmodscdrscalingicon":     5007,
    "statmodsmovementspeedicon":  5010,
    "statmodshealthplusicon":     5001,
    "statmodshealthscalingicon":  5001,
    "statmodsarmoricon":          5002,
    "statmodsmagicresicon":       5003,
    "statmodstenacityicon":       5003,
}
DEFAULT_SHARDS = [5008, 5008, 5001]

SUMMONER_MAP = {
    "summonerflash":     4,
    "summonerignite":   14,
    "summonerteleport": 12,
    "summonerexhaust":   3,
    "summonerbarrier":  21,
    "summonerheal":      7,
    "summonerboost":     1,
    "summonermana":      6,
    "summonerghost":     6,
    "summonerhaste":     6,
    "summonersmite":    11,
    "summonerdot":      14,
    "summonerporo":     30,
    "summonersnowball": 32,
    "summonermark":     32,
}

LOLALYTICS_LANES = {
    "top":     "https://lolalytics.com/lol/tierlist/?lane=top",
    "jungle":  "https://lolalytics.com/lol/tierlist/?lane=jungle",
    "mid":     "https://lolalytics.com/lol/tierlist/?lane=middle",
    "bot":     "https://lolalytics.com/lol/tierlist/?lane=bottom",
    "support": "https://lolalytics.com/lol/tierlist/?lane=support",
}

# ── JS strings (identical to ugg_api.py / role_updater.py) ───────────────

EXTRACT_JS = r"""
(async () => {
    for (let i = 0; i < 75; i++) {
        if (document.querySelector('.perk-active')) break;
        await new Promise(r => setTimeout(r, 200));
    }
    const activeImgs = [...new Set(
        [...document.querySelectorAll('.perk-active img')]
        .map(i => i.src.split('/').pop().replace(/\.(png|webp)$/i,'').toLowerCase())
    )];
    const shardImgs = [...document.querySelectorAll('.shard-active img')]
        .map(i => i.src.split('/').pop().replace(/\.(png|webp)$/i,'').toLowerCase());
    const treeImgs = [...new Set(
        [...document.querySelectorAll('img')]
        .map(i => i.src)
        .filter(s => s.match(/\/runes\/\d{4}\.png/))
        .map(s => s.match(/\/(\d{4})\.png/)[1])
    )];
    const getSummonerImgs = () => {
        const results = [];
        for (const img of document.querySelectorAll('img')) {
            const src = img.src || '';
            const alt = (img.alt || '').toLowerCase();
            let stem = null;
            const srcMatch = src.match(/\/spell\/(Summoner\w+)\.(png|webp)/i);
            if (srcMatch) {
                stem = srcMatch[1].toLowerCase();
            } else if (alt.startsWith('summoner spell ')) {
                stem = 'summoner' + alt.replace('summoner spell ', '').replace(/\s+/g, '');
            }
            if (stem && !results.includes(stem)) results.push(stem);
            if (results.length === 2) break;
        }
        return results;
    };
    let summonerImgs = [];
    for (let i = 0; i < 75; i++) {
        summonerImgs = getSummonerImgs();
        if (summonerImgs.length >= 2) break;
        await new Promise(r => setTimeout(r, 200));
    }

    // ── Item extraction via CSS sprite reverse-lookup ────────────────────────
    // u.gg renders items as divs with background-image sprite sheets.
    // We fetch item.json to build a (sprite, x, y) -> itemId map.
    await new Promise(r => setTimeout(r, 1000));

    const spriteEls = [...document.querySelectorAll('[style*="sprite/item"]')];

    // Detect patch version from the first sprite URL found on the page
    let spriteLookup = {};
    for (const el of spriteEls) {
        const bgImg = el.style.backgroundImage || '';
        const vm = bgImg.match(/riot_static\/([\d.]+)\/img\/sprite\/item/);
        if (!vm) continue;
        try {
            const itemUrl = `https://static.bigbrain.gg/assets/lol/riot_static/${vm[1]}/data/en_US/item.json`;
            const resp = await fetch(itemUrl);
            const itemData = await resp.json();
            for (const [id, item] of Object.entries(itemData.data || {})) {
                if (!item.image) continue;
                const sprite = item.image.sprite.replace(/\.(png|webp)$/, '');
                spriteLookup[`${sprite}|${item.image.x}|${item.image.y}`] = parseInt(id);
            }
        } catch(e) {}
        break;
    }

    // Find the container element for a section by locating a leaf text node
    // that starts with the keyword, then walking up to the first ancestor
    // that contains at least 2 item sprites.
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
                if (sprites.length >= 2) return cur;
                cur = cur.parentElement;
            }
        }
        return null;
    };

    const extractFromContainer = (container, maxItems) => {
        const found = [];
        if (!container) return found;
        const els = container.querySelectorAll('[style*="sprite/item"]');
        for (const el of els) {
            const bgImg = el.style.backgroundImage || '';
            const bgPos = el.style.backgroundPosition || '';
            const sm = bgImg.match(/sprite\/(item\d+)\.(webp|png)/);
            const pm = bgPos.match(/(-?\d+)px\s+(-?\d+)px/);
            if (!sm || !pm) continue;
            const itemId = spriteLookup[`${sm[1]}|${Math.abs(parseInt(pm[1]))}|${Math.abs(parseInt(pm[2]))}`];
            if (itemId && !found.includes(itemId)) found.push(itemId);
            if (found.length >= maxItems) break;
        }
        return found;
    };

    const startContainer  = findSectionContainer('starting items');
    const coreContainer   = findSectionContainer('core items');
    const fourthContainer = findSectionContainer('fourth item options');
    const fifthContainer  = findSectionContainer('fifth item options');
    const sixthContainer  = findSectionContainer('sixth item options');
    const itemsStartIds   = extractFromContainer(startContainer, 4);
    const itemsCoreIds    = extractFromContainer(coreContainer, 6).filter(id => !itemsStartIds.includes(id));
    const itemsFourthIds  = extractFromContainer(fourthContainer, 6).filter(id => !itemsStartIds.includes(id) && !itemsCoreIds.includes(id));
    const itemsFifthIds   = extractFromContainer(fifthContainer, 6).filter(id => !itemsStartIds.includes(id) && !itemsCoreIds.includes(id) && !itemsFourthIds.includes(id));
    const itemsSixthIds   = extractFromContainer(sixthContainer, 6).filter(id => !itemsStartIds.includes(id) && !itemsCoreIds.includes(id) && !itemsFourthIds.includes(id) && !itemsFifthIds.includes(id));
    const itemsStart    = itemsStartIds.map(String);
    const itemsCore     = itemsCoreIds.map(String);

    return JSON.stringify({ activeImgs, shardImgs, treeImgs, summonerImgs, itemsStart, itemsCore, itemsStartIds, itemsCoreIds, itemsFourthIds, itemsFifthIds, itemsSixthIds });
})()
"""

MATCHUP_JS = r"""
(async (enemyName) => {
    const norm = s => s.toLowerCase().replace(/['\u2019\s.'&]/g, '');
    const target = norm(enemyName);
    for (let i = 0; i < 100; i++) {
        if (document.body.innerText.length > 2000) break;
        await new Promise(r => setTimeout(r, 200));
    }
    await new Promise(r => setTimeout(r, 800));
    let winRate = null;
    const leaves = [...document.querySelectorAll('*')].filter(e => e.children.length === 0);
    for (const el of leaves) {
        if (norm(el.textContent.trim()) !== target) continue;
        let container = el;
        for (let i = 0; i < 10; i++) {
            container = container.parentElement;
            if (!container) break;
            const texts = [...container.querySelectorAll('*')]
                .filter(c => c.children.length === 0)
                .map(c => c.textContent.trim());
            for (const t of texts) {
                const m = t.match(/^(\d{2,3}\.\d{1,2})\s*%$/);
                if (m) { winRate = parseFloat(m[1]); break; }
            }
            if (winRate !== null) break;
            const cm = container.textContent.match(/(\d{2,3}\.\d{1,2})\s*%/);
            if (cm && container.textContent.length < 300) {
                winRate = parseFloat(cm[1]);
                break;
            }
        }
        if (winRate !== null) break;
    }
    if (winRate === null) {
        const lines = document.body.innerText.split('\n').map(l => l.trim()).filter(Boolean);
        for (let i = 0; i < lines.length; i++) {
            if (norm(lines[i]) !== target) continue;
            for (let j = Math.max(0, i - 3); j <= Math.min(lines.length - 1, i + 5); j++) {
                const m = lines[j].match(/^(\d{2,3}\.\d{1,2})\s*%$/) || lines[j].match(/(\d{2,3}\.\d{1,2})\s*%/);
                if (m) { winRate = parseFloat(m[1]); break; }
            }
            if (winRate !== null) break;
        }
    }
    if (winRate === null) {
        const escaped = enemyName.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
        const re = new RegExp(escaped + '[\\s\\S]{0,300}?(\\d{2,3}\\.\\d{1,2})\\s*%', 'i');
        const m = document.body.innerText.match(re);
        if (m) winRate = parseFloat(m[1]);
    }
    return JSON.stringify({ winRate });
})('%%ENEMY%%')
"""

LOLALYTICS_JS = r"""
(async () => {
    await new Promise(r => setTimeout(r, 2000));
    for (let i = 0; i < 30; i++) {
        window.scrollBy(0, 600);
        await new Promise(r => setTimeout(r, 150));
    }
    window.scrollTo(0, 0);
    await new Promise(r => setTimeout(r, 500));
    let prev = 0, links = [];
    for (let i = 0; i < 60; i++) {
        links = [...document.querySelectorAll('a[href*="/lol/"][href*="/build/"]')];
        if (links.length > 20 && links.length === prev) break;
        prev = links.length;
        await new Promise(r => setTimeout(r, 300));
    }
    if (links.length === 0) return JSON.stringify({});
    await new Promise(r => setTimeout(r, 300));
    const results = {};
    for (const link of links) {
        const m = link.href.match(/\/lol\/([\w-]+)\/build\//);
        if (!m || results[m[1]] !== undefined) continue;
        const slug = m[1];
        let rowEl = null;
        let el = link.parentElement;
        for (let d = 0; d < 15 && el && el !== document.body; d++, el = el.parentElement) {
            const n = el.querySelectorAll('a[href*="/lol/"][href*="/build/"]').length;
            if (n > 3) break;
            if (n >= 1 && (el.innerText || '').length >= 15) rowEl = el;
        }
        if (!rowEl) continue;
        const text = rowEl.innerText.trim();
        const m2 = text.match(/\b(\d{1,3}\.\d{1,2})\b/);
        if (m2) {
            const pct = parseFloat(m2[1]);
            if (pct >= 0.2 && pct <= 100.0) results[slug] = pct;
        }
    }
    return JSON.stringify(results);
})()
"""

CHAMP_ROLE_JS = r"""
(async () => {
    // Wait for the page to hydrate — role tabs appear after JS runs
    for (let i = 0; i < 75; i++) {
        if (document.body.innerText.length > 500) break;
        await new Promise(r => setTimeout(r, 200));
    }
    await new Promise(r => setTimeout(r, 1500));

    const LANE_KEYS = {
        'top': 'top', 'jungle': 'jungle', 'jng': 'jungle',
        'mid': 'mid', 'middle': 'mid',
        'adc': 'bot', 'bot': 'bot', 'bottom': 'bot',
        'support': 'support', 'sup': 'support', 'supp': 'support'
    };

    const result = {};

    // Strategy 1: lane tab links — LoLalytics renders tabs as
    // <a href="/lol/kaisa/?lane=mid">…<span>1.8%</span></a>
    for (const a of document.querySelectorAll('a[href]')) {
        const href = a.href.toLowerCase();
        let lane = null;
        for (const [key, val] of Object.entries(LANE_KEYS)) {
            if (href.includes('lane=' + key)) { lane = val; break; }
        }
        if (!lane || result[lane] !== undefined) continue;

        // Walk up to 3 levels to find a percentage leaf near this link
        let el = a;
        outer: for (let d = 0; d < 3; d++) {
            const leaves = [...el.querySelectorAll('*')].filter(e => e.children.length === 0);
            for (const leaf of [el, ...leaves]) {
                const txt = (leaf.textContent || '').trim();
                const m = txt.match(/^(\d{1,3}(?:\.\d{1,2})?)%$/);
                if (m) {
                    const pct = parseFloat(m[1]);
                    if (pct >= 0.1 && pct <= 100) { result[lane] = pct; break outer; }
                }
            }
            el = el.parentElement;
            if (!el || el === document.body) break;
        }
    }

    // Strategy 2: text scan of the first 3000 chars for "LaneName XX.X%" patterns
    // Catches layouts where percentages aren't inside the link element itself
    if (Object.keys(result).length < 2) {
        const topText = document.body.innerText.slice(0, 3000);
        for (const [key, val] of Object.entries(LANE_KEYS)) {
            if (result[val] !== undefined) continue;
            const re = new RegExp('\\b' + key + '\\b[\\s\\S]{0,40}?(\\d{1,3}(?:\\.\\d{1,2})?)%', 'i');
            const m = topText.match(re);
            if (m) {
                const pct = parseFloat(m[1]);
                if (pct >= 0.1 && pct <= 100) result[val] = pct;
            }
        }
    }

    return JSON.stringify(result);
})()
"""

COUNTERS_JS = r"""
(async () => {
    for (let i = 0; i < 75; i++) {
        if (document.body.innerText.length > 3000) break;
        await new Promise(r => setTimeout(r, 200));
    }
    await new Promise(r => setTimeout(r, 500));
    const results = [];
    const fullText = document.body.innerText;
    const bestStart = fullText.indexOf('Best Picks vs');
    const bestEnd   = fullText.indexOf('Worst Picks vs');
    if (bestStart === -1) return JSON.stringify(results);
    const section = fullText.slice(bestStart, bestEnd !== -1 ? bestEnd : bestStart + 3000);
    const lines = section.split('\n').map(l => l.trim()).filter(Boolean);
    for (let i = 0; i < lines.length - 1; i++) {
        const wrMatch = lines[i].match(/^(\d{2,3}\.\d{1,2})%\s*WR$/);
        if (wrMatch && i > 0) {
            const name = lines[i - 1];
            const wr   = parseFloat(wrMatch[1]);
            if (name.length >= 2 && !name.includes('%') && wr >= 40 && wr <= 70) {
                results.push({ champion: name, winRate: wr });
            }
        }
        if (results.length >= 10) break;
    }
    return JSON.stringify(results);
})()
"""

# ── slug helpers ───────────────────────────────────────────────────────────

def _champ_slug(name: str) -> str:
    s = name.lower()
    s = s.replace("'", "").replace(".", "")
    s = re.sub(r"[^a-z0-9\s-]", "", s)
    s = re.sub(r"[\s]+", "-", s.strip())
    s = re.sub(r"-+", "-", s)
    return s


# Map LoLalytics URL slugs -> proper champion names (same as role_updater.py)
SLUG_TO_NAME: dict[str, str] = {
    "aatrox": "Aatrox", "ahri": "Ahri", "akali": "Akali", "akshan": "Akshan",
    "alistar": "Alistar", "ambessa": "Ambessa", "amumu": "Amumu",
    "anivia": "Anivia", "annie": "Annie", "aphelios": "Aphelios",
    "ashe": "Ashe", "aurelionsol": "Aurelion Sol", "aurora": "Aurora",
    "azir": "Azir", "bard": "Bard", "belveth": "Bel'Veth",
    "blitzcrank": "Blitzcrank", "brand": "Brand", "braum": "Braum",
    "briar": "Briar", "caitlyn": "Caitlyn", "camille": "Camille",
    "cassiopeia": "Cassiopeia", "chogath": "Cho'Gath", "corki": "Corki",
    "darius": "Darius", "diana": "Diana", "drmundo": "Dr. Mundo",
    "draven": "Draven", "ekko": "Ekko", "elise": "Elise",
    "evelynn": "Evelynn", "ezreal": "Ezreal", "fiddlesticks": "Fiddlesticks",
    "fiora": "Fiora", "fizz": "Fizz", "galio": "Galio",
    "gangplank": "Gangplank", "garen": "Garen", "gnar": "Gnar",
    "gragas": "Gragas", "graves": "Graves", "gwen": "Gwen",
    "hecarim": "Hecarim", "heimerdinger": "Heimerdinger", "hwei": "Hwei",
    "illaoi": "Illaoi", "irelia": "Irelia", "ivern": "Ivern",
    "janna": "Janna", "jarvaniv": "Jarvan IV", "jax": "Jax",
    "jayce": "Jayce", "jhin": "Jhin", "jinx": "Jinx",
    "ksante": "K'Sante", "kaisa": "Kai'Sa", "kalista": "Kalista",
    "karma": "Karma", "karthus": "Karthus", "kassadin": "Kassadin",
    "katarina": "Katarina", "kayle": "Kayle", "kayn": "Kayn",
    "kennen": "Kennen", "khazix": "Kha'Zix", "kindred": "Kindred",
    "kled": "Kled", "kogmaw": "Kog'Maw", "leblanc": "LeBlanc",
    "leesin": "Lee Sin", "leona": "Leona", "lillia": "Lillia",
    "lissandra": "Lissandra", "lucian": "Lucian", "lulu": "Lulu",
    "lux": "Lux", "malphite": "Malphite", "malzahar": "Malzahar",
    "maokai": "Maokai", "masteryi": "Master Yi", "mel": "Mel",
    "milio": "Milio", "missfortune": "Miss Fortune", "mordekaiser": "Mordekaiser",
    "morgana": "Morgana", "naafiri": "Naafiri", "nami": "Nami",
    "nasus": "Nasus", "nautilus": "Nautilus", "neeko": "Neeko",
    "nidalee": "Nidalee", "nilah": "Nilah", "nocturne": "Nocturne",
    "nunu": "Nunu & Willump", "olaf": "Olaf", "orianna": "Orianna",
    "ornn": "Ornn", "pantheon": "Pantheon", "poppy": "Poppy",
    "pyke": "Pyke", "qiyana": "Qiyana", "quinn": "Quinn",
    "rakan": "Rakan", "rammus": "Rammus", "reksai": "Rek'Sai",
    "rell": "Rell", "renataglassc": "Renata Glasc", "renekton": "Renekton",
    "rengar": "Rengar", "riven": "Riven", "rumble": "Rumble",
    "ryze": "Ryze", "samira": "Samira", "sejuani": "Sejuani",
    "senna": "Senna", "seraphine": "Seraphine", "sett": "Sett",
    "shaco": "Shaco", "shen": "Shen", "shyvana": "Shyvana",
    "singed": "Singed", "sion": "Sion", "sivir": "Sivir",
    "skarner": "Skarner", "smolder": "Smolder", "sona": "Sona",
    "soraka": "Soraka", "swain": "Swain", "sylas": "Sylas",
    "syndra": "Syndra", "tahmkench": "Tahm Kench", "taliyah": "Taliyah",
    "talon": "Talon", "taric": "Taric", "teemo": "Teemo",
    "thresh": "Thresh", "tristana": "Tristana", "trundle": "Trundle",
    "tryndamere": "Tryndamere", "twistedfate": "Twisted Fate", "twitch": "Twitch",
    "udyr": "Udyr", "urgot": "Urgot", "varus": "Varus",
    "vayne": "Vayne", "veigar": "Veigar", "velkoz": "Vel'Koz",
    "vex": "Vex", "vi": "Vi", "viego": "Viego", "viktor": "Viktor",
    "vladimir": "Vladimir", "volibear": "Volibear", "warwick": "Warwick",
    "wukong": "Wukong", "xayah": "Xayah", "xerath": "Xerath",
    "xinzhao": "Xin Zhao", "yasuo": "Yasuo", "yone": "Yone",
    "yorick": "Yorick", "yunara": "Yunara", "yuumi": "Yuumi",
    "zaahen": "Zaahen", "zac": "Zac", "zed": "Zed", "zeri": "Zeri",
    "ziggs": "Ziggs", "zilean": "Zilean", "zoe": "Zoe", "zyra": "Zyra",
}


def _slug_to_name(slug: str) -> Optional[str]:
    clean = slug.lower().replace("-", "").replace("'", "").replace(".", "").replace(" ", "")
    return SLUG_TO_NAME.get(clean)


# ── module state ───────────────────────────────────────────────────────────

_browser = None
_perk_map: dict = {}


async def init(playwright) -> None:
    global _browser, _perk_map
    _browser = await playwright.chromium.launch(headless=True)
    _perk_map = await _fetch_perk_map()
    print("[scraper] Browser started, perk map loaded.", flush=True)


async def shutdown() -> None:
    global _browser
    if _browser:
        await _browser.close()
        _browser = None


async def _fetch_perk_map() -> dict:
    """Fetch CommunityDragon perk data: filename -> ID."""
    import asyncio
    ssl_ctx = ssl.create_default_context()
    url = ("https://raw.communitydragon.org/latest/plugins/"
           "rcp-be-lol-game-data/global/default/v1/perks.json")
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    loop = asyncio.get_event_loop()
    data = await loop.run_in_executor(
        None,
        lambda: json.loads(urllib.request.urlopen(req, context=ssl_ctx, timeout=10).read())
    )
    perk_map = {}
    for p in data:
        fname = p["iconPath"].split("/")[-1].lower()
        fname = fname.replace(".png", "").replace(".webp", "")
        perk_map[fname] = p["id"]
    return perk_map


async def _new_context():
    """Create a new browser context with a realistic user agent."""
    return await _browser.new_context(
        user_agent=(
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/124.0.0.0 Safari/537.36"
        ),
        viewport={"width": 1280, "height": 800},
    )


# ── public scrape functions ────────────────────────────────────────────────

async def scrape_build(champion: str, role: str) -> Optional[dict]:
    """
    Scrape u.gg build page. Returns a dict with the same shape as the old
    UGGClient.get_top_build(): primary_style_id, sub_style_id, selected_perk_ids,
    summoners, role, champion.
    """
    role_slug = ROLE_MAP.get(role.lower(), "")
    url = f"https://u.gg/lol/champions/{_champ_slug(champion)}/build"
    if role_slug:
        url += f"?role={role_slug}"

    ctx = await _new_context()
    page = await ctx.new_page()
    page.set_default_timeout(60000)
    try:
        await page.goto(url, wait_until="domcontentloaded", timeout=30000)
        raw_str = await page.evaluate(EXTRACT_JS)
        raw = json.loads(raw_str) if isinstance(raw_str, str) else (raw_str or {})
    finally:
        await ctx.close()

    active_imgs      = raw.get("activeImgs", [])
    shard_imgs       = raw.get("shardImgs", [])
    tree_raw         = raw.get("treeImgs", [])
    items_start      = raw.get("itemsStart", [])
    items_core       = raw.get("itemsCore", [])
    items_start_ids  = raw.get("itemsStartIds", [])
    items_core_ids   = raw.get("itemsCoreIds", [])
    items_fourth_ids = raw.get("itemsFourthIds", [])
    items_fifth_ids  = raw.get("itemsFifthIds", [])
    items_sixth_ids  = raw.get("itemsSixthIds", [])

    SHARD_ID_SET = set(SHARD_IDS.values())

    perk_ids = []
    for fname in active_imgs:
        pid = _perk_map.get(fname)
        if pid and pid not in perk_ids and pid not in SHARD_ID_SET:
            perk_ids.append(pid)

    shard_perk_ids = []
    for fname in shard_imgs:
        pid = SHARD_IDS.get(fname)
        if pid:
            shard_perk_ids.append(pid)
    while len(shard_perk_ids) < 3:
        shard_perk_ids.append(DEFAULT_SHARDS[len(shard_perk_ids)])
    shard_perk_ids = shard_perk_ids[:3]

    selected_perk_ids = perk_ids[:6] + shard_perk_ids

    tree_style_ids  = [int(t) for t in tree_raw if t.isdigit()]
    primary_style   = tree_style_ids[0] if tree_style_ids else 8000
    secondary_style = tree_style_ids[1] if len(tree_style_ids) > 1 else 8100

    summoner_imgs = raw.get("summonerImgs", [])
    summoner_ids  = [SUMMONER_MAP[f] for f in summoner_imgs if f in SUMMONER_MAP]

    print(f"[scraper] build {champion}/{role}: perks={perk_ids}, shards={shard_perk_ids}, "
          f"trees={tree_style_ids}, spells={summoner_ids}, "
          f"starter={items_start}, core={items_core}", flush=True)

    if len(selected_perk_ids) != 9:
        raise RuntimeError(
            f"Got {len(selected_perk_ids)}/9 runes for {champion} "
            f"(perk_ids={perk_ids}, shards={shard_perk_ids}). "
            "u.gg may not have loaded correctly."
        )

    return {
        "champion":          champion,
        "role":              role_slug or role,
        "primary_style_id":  primary_style,
        "sub_style_id":      secondary_style,
        "selected_perk_ids": selected_perk_ids,
        "items_start":       items_start,
        "items_core":        items_core,
        "items_start_ids":   items_start_ids,
        "items_core_ids":    items_core_ids,
        "items_fourth_ids":  items_fourth_ids,
        "items_fifth_ids":   items_fifth_ids,
        "items_sixth_ids":   items_sixth_ids,
        "summoners":         summoner_ids,
        "skill_order":       [],
    }


async def scrape_counters(enemy_champ: str, role: str, top_n: int = 5) -> list:
    """Scrape u.gg counters page. Returns list of {champion, win_rate}."""
    role_slug = ROLE_MAP.get(role.lower(), "")
    url = f"https://u.gg/lol/champions/{_champ_slug(enemy_champ)}/counter"
    if role_slug:
        url += f"?role={role_slug}"

    ctx = await _new_context()
    page = await ctx.new_page()
    page.set_default_timeout(60000)
    try:
        await page.goto(url, wait_until="domcontentloaded", timeout=30000)

        # 404 guard
        title = await page.title()
        if "not found" in title.lower():
            print(f"[scraper] counters 404 for {enemy_champ}", flush=True)
            return []

        raw_str = await page.evaluate(COUNTERS_JS)
        raw = json.loads(raw_str) if isinstance(raw_str, str) else (raw_str or [])
    finally:
        await ctx.close()

    if not isinstance(raw, list):
        return []

    clean = [
        {"champion": e["champion"], "win_rate": e["winRate"]}
        for e in raw
        if isinstance(e.get("champion"), str) and len(e["champion"]) >= 2
        and isinstance(e.get("winRate"), (int, float))
        and 40 <= e["winRate"] <= 70
    ]
    clean.sort(key=lambda x: x["win_rate"], reverse=True)
    return clean[:top_n]


async def scrape_matchup(my_champ: str, enemy_champ: str, role: str) -> Optional[dict]:
    """Scrape u.gg matchup win rate. Returns {win_rate, enemy} or None."""
    role_slug = ROLE_MAP.get(role.lower(), "")
    url = f"https://u.gg/lol/champions/{_champ_slug(my_champ)}/matchups"
    if role_slug:
        url += f"?role={role_slug}"

    ctx = await _new_context()
    page = await ctx.new_page()
    page.set_default_timeout(60000)
    try:
        await page.goto(url, wait_until="domcontentloaded", timeout=30000)
        enemy_safe = enemy_champ.replace("'", "\\'").replace("\\", "\\\\")
        js = MATCHUP_JS.replace("%%ENEMY%%", enemy_safe)
        raw_str = await page.evaluate(js)
        raw = json.loads(raw_str) if isinstance(raw_str, str) else (raw_str or {})
    finally:
        await ctx.close()

    wr = raw.get("winRate") if raw else None
    print(f"[scraper] matchup {my_champ} vs {enemy_champ}: {wr}", flush=True)
    if wr is None:
        return None
    return {"win_rate": wr, "enemy": enemy_champ}


async def scrape_role_weights() -> dict:
    """
    Scrape each champion's individual LoLalytics page for their full role
    distribution. Unlike the old tier-list approach, this captures off-meta
    flex picks (e.g. Kai'Sa mid at 1.8%) that never appear on lane tier lists.

    Runs up to 5 pages in parallel — total time ~2-3 min for all champions.
    Returns: {"ChampName": {"top": 15.4, "mid": 1.8, ...}, ...}
    """
    combined: dict[str, dict[str, float]] = {}
    all_champs = list(SLUG_TO_NAME.items())   # [(slug, name), ...]
    total = len(all_champs)
    sem = asyncio.Semaphore(5)

    async def _scrape_one(slug: str, name: str) -> tuple[str, dict]:
        async with sem:
            url = f"https://lolalytics.com/lol/{slug}/"
            ctx = await _new_context()
            page = await ctx.new_page()
            page.set_default_timeout(25000)
            try:
                await page.goto(url, wait_until="domcontentloaded", timeout=20000)
                raw_str = await page.evaluate(CHAMP_ROLE_JS)
                raw = json.loads(raw_str) if isinstance(raw_str, str) else {}
                roles = {
                    role: round(float(pct), 2)
                    for role, pct in raw.items()
                    if isinstance(pct, (int, float)) and 0.2 <= float(pct) <= 100.0
                }
                return name, roles
            except Exception as e:
                print(f"[scraper] role weights — {name} failed: {e}", flush=True)
                return name, {}
            finally:
                await ctx.close()

    print(f"[scraper] role weights — scraping {total} champion pages (5 parallel)...", flush=True)
    tasks = [_scrape_one(slug, name) for slug, name in all_champs]
    results = await asyncio.gather(*tasks)

    for name, roles in results:
        if roles:
            combined[name] = roles

    print(f"[scraper] role weights — done: {len(combined)}/{total} champions", flush=True)
    return combined
