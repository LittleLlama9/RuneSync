
# JS to extract win rate for a specific enemy from u.gg matchups page
MATCHUP_JS_TEMPLATE = r"""
(async (enemyName) => {
    // Wait for matchup rows to load
    for (let i = 0; i < 100; i++) {
        if (document.querySelector('[class*="matchup-row"], [class*="champion-name"], .champion-name')) break;
        await new Promise(r => setTimeout(r, 200));
    }
    await new Promise(r => setTimeout(r, 800));

    const name = enemyName.toLowerCase().replace(/['\s]/g, '').replace('&', '');

    // Try to find a row containing the enemy champ name
    const allText = document.querySelectorAll('*');
    let winRate = null;
    let gamesPlayed = null;

    // u.gg matchup page: rows have champion name + win rate % close together
    // Look for elements with just a % number near the enemy name
    for (const el of allText) {
        const txt = el.textContent.trim().toLowerCase().replace(/['\s]/g, '').replace('&', '');
        if (txt === name && el.children.length === 0) {
            // Walk up to find the row container, then find win rate
            let row = el;
            for (let i = 0; i < 6; i++) {
                row = row.parentElement;
                if (!row) break;
                const pctEl = row.querySelector('[class*="win-rate"], [class*="winrate"]');
                if (pctEl) {
                    const m = pctEl.textContent.match(/(\d+\.?\d*)\s*%/);
                    if (m) { winRate = parseFloat(m[1]); break; }
                }
                // fallback: any % text in row
                const m = row.textContent.match(/(\d{2}\.\d)\s*%/);
                if (m && !winRate) winRate = parseFloat(m[1]);
            }
            if (winRate) break;
        }
    }

    // Fallback: scan all text nodes for pattern "[EnemyName]...XX.X%"
    if (!winRate) {
        const body = document.body.innerText;
        const re = new RegExp(enemyName.replace(/[.*+?^${}()|[\]\\]/g,'\\$&') + '[\\s\\S]{0,120}?(\\d{2}\\.\\d)\\s*%', 'i');
        const m = body.match(re);
        if (m) winRate = parseFloat(m[1]);
    }

    return JSON.stringify({ winRate, gamesPlayed });
})('ENEMY_NAME')
"""
