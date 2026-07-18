/* DAEMON frontend logic — state-driven render + routing + theme + keys.
   Seeded with placeholder data (preview/standalone); under pywebview, get_state
   hydrates and a 200ms poll loop drains live events via poll_events() -> handlePush. */
(function () {
  'use strict';

  const PHOSPHORS = ['amber', 'green', 'ice'];
  const INTERFACES = [['Standard', 'standard'], ['DAEMON Classic', 'classic']];
  const RANKS = ['Iron+', 'Bronze+', 'Silver+', 'Gold+', 'Platinum+', 'Emerald+', 'Diamond+', 'Master+'];
  const REGIONS = ['World', 'NA', 'EUW', 'EUNE', 'KR', 'BR', 'JP', 'OCE', 'LAS', 'LAN', 'TR', 'RU'];
  const PROMPTS = {
    monitor: 'watch --champ-select', builds: 'edit builds.ledger',
    settings: 'vim daemon.conf', history: 'query match.history',
    report: 'cat postgame.report', editor: 'vim override', builder: 'edit build',
    debug: 'tail -f runesync.log'
  };
  const STANDARD_PROMPTS = {
    monitor: 'Monitoring champion select', builds: 'Manage custom builds',
    settings: 'Application preferences', history: 'Local match archive',
    report: 'Performance breakdown', editor: 'Edit custom build',
    builder: 'Choose build items', debug: 'Diagnostics'
  };
  // static game data (mirrors lcu.py; rarely changes)
  const ROLES = ['auto', 'top', 'jungle', 'mid', 'bot', 'support'];
  const TREES = ['Precision', 'Domination', 'Sorcery', 'Resolve', 'Inspiration'];
  const KEYSTONES = {
    Precision: ['Press the Attack', 'Lethal Tempo', 'Fleet Footwork', 'Conqueror'],
    Domination: ['Electrocute', 'Predator', 'Dark Harvest', 'Hail of Blades'],
    Sorcery: ['Summon Aery', 'Arcane Comet', 'Phase Rush'],
    Resolve: ['Grasp of the Undying', 'Aftershock', 'Guardian'],
    Inspiration: ['Glacial Augment', 'First Strike', 'Unsealed Spellbook']
  };
  const SPELLS = [
    ['— (u.gg default)', 0], ['Flash', 4], ['Ignite', 14], ['Exhaust', 3], ['Barrier', 21],
    ['Heal', 7], ['Ghost', 6], ['Teleport', 12], ['Cleanse', 1], ['Smite', 11], ['Clarity', 13]
  ];
  const SPELL_NAME = id => (SPELLS.find(s => s[1] === (id || 0)) || SPELLS[0])[0];
  const SPELL_ICON_FILE = {
    1: 'SummonerBoost.png', 3: 'SummonerExhaust.png', 4: 'SummonerFlash.png',
    6: 'SummonerHaste.png', 7: 'SummonerHeal.png', 11: 'SummonerSmite.png',
    12: 'SummonerTeleport.png', 13: 'SummonerMana.png', 14: 'SummonerDot.png',
    21: 'SummonerBarrier.png'
  };
  const spellIconUrl = id => SPELL_ICON_FILE[id] ? `assets/spells/${SPELL_ICON_FILE[id]}` : '';
  const championIconUrl = id => id
    ? `https://raw.communitydragon.org/latest/plugins/rcp-be-lol-game-data/global/default/v1/champion-icons/${Number(id)}.png`
    : '';
  const titleCase = value => {
    const text = String(value || '');
    return text ? text.charAt(0).toUpperCase() + text.slice(1) : '';
  };
  const standardInterface = () =>
    document.documentElement.getAttribute('data-interface') === 'standard';
  const plainMeta = value => {
    const text = String(value || '').replace(/^\[\s*|\s*\]$/g, '');
    return titleCase(text);
  };

  // ── state (placeholder seed = the mock's sample data) ─────────────────────
  const state = {
    screen: 'monitor',
    status: 'waiting',                 // booting|connecting|connected|monitoring|waiting
    monitoring: false,
    inGame: false,
    champ: 'JINX', champMeta: '[ locked · bottom lane · marksman ]', imported: true,
    selecting: false, _logAnchorCount: 0,
    enemy: 'CAITLYN', wr: 52.8, wrLabel: 'FAVORABLE', wrTag: 'success',
    sample: 'PLATINUM+ · WORLD',
    runes: {
      keystone: 'LETHAL TEMPO', primary: 'Precision', secondary: 'Domination',
      primaryMinor: 'Triumph · Alacrity · Cut Down',
      secondaryMinor: 'Sudden Impact · Treasure Hunter',
      summoners: 'FLASH / HEAL'
    },
    buildSrc: 'u.gg',
    duo: null,
    build: [
      { i: 1, name: "Doran's Blade", tag: 'start', icon: 'https://ddragon.leagueoflegends.com/cdn/15.6.1/img/item/1055.png' },
      { i: 2, name: 'Health Potion', tag: 'start', icon: 'https://ddragon.leagueoflegends.com/cdn/15.6.1/img/item/2003.png' },
      { i: 3, name: 'Health Potion', tag: 'start', icon: 'https://ddragon.leagueoflegends.com/cdn/15.6.1/img/item/2003.png' },
      { i: 4, name: 'Kraken Slayer', tag: 'core ←', core: true, icon: 'https://ddragon.leagueoflegends.com/cdn/15.6.1/img/item/6672.png' },
      { i: 5, name: "Berserker's Greaves", tag: 'boots', icon: 'https://ddragon.leagueoflegends.com/cdn/15.6.1/img/item/3006.png' },
      { i: 6, name: 'Infinity Edge', tag: 'core', icon: 'https://ddragon.leagueoflegends.com/cdn/15.6.1/img/item/3031.png' }
    ],
    log: [
      { ts: '14:31:58', cls: '', msg: 'connected to league client' },
      { ts: '14:32:02', cls: '', msg: 'champ select detected — watching picks' },
      { ts: '14:32:06', cls: '', msg: 'jinx locked (bot) — fetching precision × domination' },
      { ts: '14:32:07', cls: 'ok', msg: '>> runes imported · summoners set · build queued' },
      { ts: '14:32:09', cls: '', msg: 'matchup vs caitlyn resolved — 52.8% favorable' }
    ],
    builds: [
      { champ: 'Yasuo', role: 'mid', path: 'Precision × Resolve', summoners: 'Flash / Ignite' },
      { champ: 'Thresh', role: 'support', path: 'Resolve × Inspiration', summoners: 'Flash / Exhaust' },
      { champ: 'Lee Sin', role: 'jungle', path: 'Precision × Domination', summoners: 'Flash / Smite' },
      { champ: 'Ezreal', role: 'bottom', path: 'Precision × Inspiration', summoners: 'Flash / Teleport' }
    ],
    sel: 0,
    history: {
      loaded: false, loading: false, syncing: false, error: '', offset: 0,
      rows: [], hasMore: true, section: 'matches',
      summary: {
        overall: {}, recent20: {}, champions: [], roles: [], performance: {}
      }
    },
    report: null,
    settings: {
      rank: 'Platinum+', region: 'World', auto_role: true, trigger: 'hover',
      phosphor: 'amber', interface_style: 'standard', score_v2_beta: true,
      score_v2_beta_sources: [], score_v2_beta_error: '', autostart: false
    }
  };

  const $ = (id) => document.getElementById(id);
  const esc = (s) => String(s).replace(/[&<>"']/g,
    c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));

  // ── render ────────────────────────────────────────────────────────────────
  function renderStatus() {
    const el = $('status');
    el.classList.toggle('waiting', state.status === 'waiting');
    $('statusLabel').textContent = 'LEAGUE: ' + state.status.toUpperCase();
    el.setAttribute('aria-label', `Toggle monitoring. League ${state.status}`);
  }

  function renderMonitor() {
    const standard = standardInterface();
    const selecting = state.selecting && !state.champ;
    $('champName').textContent = state.champ || (selecting ? 'SELECTING…' : '—');
    $('champMeta').textContent = state.champ
      ? (standard ? plainMeta(state.champMeta) : (state.champMeta || ''))
      : (selecting
          ? (standard ? 'Pick a champion to load your setup' : '[ in champ select · pick a champion ]')
          : (standard ? 'Waiting for champion select' : (state.champMeta || '[ awaiting champ select ]')));
    $('champBadge').hidden = !state.imported;

    $('matchupTitle').textContent = standard
      ? (state.enemy ? `Matchup vs ${state.enemy}` : 'Matchup')
      : (state.enemy
          ? `MATCHUP // vs ${state.enemy}`
          : (state.selecting ? 'MATCHUP // awaiting pick' : 'MATCHUP // idle'));
    const down = state.wr != null && state.wr < 50;
    $('wrNum').innerHTML = state.wr == null ? '—' : `${state.wr.toFixed(1)}<small>%</small>`;
    const dir = $('wrDir');
    dir.textContent = state.wr == null
      ? (state.enemy ? (state.wrLabel || (standard ? 'Win rate unavailable' : 'win rate unavailable'))
                     : (state.selecting
                        ? (standard ? 'Waiting for your pick' : 'awaiting pick')
                        : (standard ? 'Waiting for matchup' : 'awaiting matchup')))
      : `${down ? '▼' : '▲'} ${state.wrLabel || ''}`.trim();
    dir.classList.toggle('down', !!down);
    const fill = $('wrFill');
    fill.style.width = (state.wr == null ? 0 : Math.max(0, Math.min(100, state.wr))) + '%';
    fill.classList.toggle('down', !!down);
    $('wrSample').textContent = state.wr == null ? '' : state.sample;

    const r = state.runes;
    $('rKeystone').textContent = r.keystone || '—';
    $('rPrimary').textContent = r.primary || '—';
    $('rPrimaryMinor').textContent = r.primaryMinor || '';
    $('rSecondary').textContent = r.secondary || '—';
    $('rSecondaryMinor').textContent = r.secondaryMinor || '';
    $('rSummoners').textContent = r.summoners || '—';

    $('buildTitle').textContent = standard
      ? (state.buildSrc === 'idle' ? 'Recommended build' : `Recommended build · ${state.buildSrc}`)
      : `BUILD // ${state.buildSrc}`;
    $('buildList').innerHTML = renderBuildList(state.build);

    renderDuo();
    renderHud();
    renderLog();
  }

  function renderDuo() {
    const standard = standardInterface();
    const panel = $('duoPanel');
    if (!panel) return;
    const duo = state.duo;
    const recs = (duo && Array.isArray(duo.recs)) ? duo.recs : [];
    if (!duo || !recs.length) { panel.hidden = true; return; }
    panel.hidden = false;
    const myRole = (duo.myRole || '').toUpperCase();
    $('duoTitle').textContent = standard
      ? `Best ${titleCase(duo.myRole)} pairs with ${duo.partner}`
      : `BEST PAIRS // ${myRole} × ${(duo.partner || '').toUpperCase()}`;
    $('duoList').innerHTML = recs.map((r, i) => {
      const wr = (typeof r.win_rate === 'number') ? r.win_rate.toFixed(1) : '—';
      const tier = esc(r.tier || '');
      const label = esc(r.tier_label || '');
      const games = r.games ? `<span class="duo-games">${fmtGames(r.games)} games</span>` : '';
      return `<div class="duo-row">` +
        `<span class="duo-rank">${i + 1}</span>` +
        `<span class="duo-champ">${esc(r.champion || '')}</span>` +
        `<span class="duo-tier tier-${tier}" title="${label}">${tier}</span>` +
        `<span class="duo-wr">${wr}<small>%</small></span>` +
        games +
        `</div>`;
    }).join('');
    $('duoSample').textContent = duo.sample || '';
  }

  function mmss(sec) {
    sec = Math.max(0, Math.round(Number(sec) || 0));
    const m = Math.floor(sec / 60), s = sec % 60;
    return `${m}:${String(s).padStart(2, '0')}`;
  }
  function signed(n) { n = Number(n) || 0; return (n > 0 ? '+' : '') + n; }

  function renderHud() {
    const panel = $('hudPanel');
    if (!panel) return;
    const hud = state.hud;
    if (!state.inGame || !hud || !hud.me) { panel.hidden = true; return; }
    panel.hidden = false;
    const standard = standardInterface();
    $('hudTitle').textContent = standard
      ? `Live · ${mmss(hud.game_time)}`
      : `LIVE // ${mmss(hud.game_time)}`;

    const me = hud.me, opp = hud.opponent, d = hud.delta;
    const rows = [];

    // CS / min — the core farming feedback line.
    const oppCs = opp ? `${esc(opp.champion)} ${opp.cs} (${opp.cs_per_min.toFixed(1)})` : '—';
    const csDelta = d ? `<span class="hud-delta ${d.cs >= 0 ? 'up' : 'down'}">${signed(d.cs)}</span>` : '';
    rows.push(
      `<div class="hud-row"><span class="hud-k">CS</span>` +
      `<span class="hud-v">You ${me.cs} (${me.cs_per_min.toFixed(1)}/m) · vs ${oppCs} ${csDelta}</span></div>`);

    // Gold — lane estimate (from held items) + team total.
    if (d || hud.team_gold) {
      const laneG = d ? `<span class="hud-delta ${d.gold >= 0 ? 'up' : 'down'}">${signed(d.gold)}g lane</span>` : '';
      const teamG = hud.team_gold
        ? `<span class="hud-delta ${hud.team_gold.diff >= 0 ? 'up' : 'down'}">${signed(hud.team_gold.diff)}g team</span>` : '';
      rows.push(`<div class="hud-row"><span class="hud-k">GOLD</span><span class="hud-v">${laneG} · ${teamG}</span></div>`);
    }

    // Level.
    if (opp && d) {
      const lvlDelta = `<span class="hud-delta ${d.level >= 0 ? 'up' : 'down'}">${signed(d.level)}</span>`;
      rows.push(`<div class="hud-row"><span class="hud-k">LVL</span><span class="hud-v">${me.level} vs ${opp.level} ${lvlDelta}</span></div>`);
    }

    // Objective timers.
    const objs = (hud.objectives || []).map(o => {
      let t;
      if (o.state === 'gone') t = '—';
      else if (o.next_seconds == null) t = 'up';
      else t = mmss(o.next_seconds);
      const cls = (o.next_seconds == null && o.state !== 'gone') ? 'up' : '';
      return `<span class="hud-obj"><b>${esc(o.name)}</b> <span class="${cls}">${t}</span></span>`;
    }).join('');
    if (objs) rows.push(`<div class="hud-row hud-objs"><span class="hud-k">OBJ</span><span class="hud-v">${objs}</span></div>`);

    $('hudBody').innerHTML = rows.join('');
  }

  function fmtGames(n) {
    n = Number(n) || 0;
    return n >= 1000 ? (n / 1000).toFixed(n >= 10000 ? 0 : 1) + 'k' : String(n);
  }

  function renderBuildList(items) {
    if (!items.length) return `<div class="brow"><span class="bi">—</span></div>`;
    const starters = items.filter(b => b.tag === 'start');
    const rest = items.filter(b => b.tag !== 'start');
    const rows = [];

    if (starters.length) {
      const seen = [], counts = {};
      for (const s of starters) {
        if (!counts[s.name]) { seen.push(s); counts[s.name] = 0; }
        counts[s.name]++;
      }
      const parts = seen.map(s => {
        const img = s.icon ? `<img class="bitem-icon" src="${esc(s.icon)}" onerror="this.style.display='none'">` : '';
        const count = counts[s.name] > 1 ? `<span class="bcount">${counts[s.name]}x</span>` : '';
        return `${img}<span class="bitem-name">${esc(s.name)}</span>${count}`;
      });
      rows.push(`<div class="brow brow-start"><span class="btag">start</span>${parts.join('<span class="bsep"> + </span>')}</div>`);
    }

    let coreN = 0;
    for (const b of rest) {
      coreN++;
      const img = b.icon ? `<img class="bitem-icon" src="${esc(b.icon)}" onerror="this.style.display='none'">` : '';
      rows.push(`<div class="brow${b.core ? ' core' : ''}">` +
        `<span class="bi">${coreN}</span>&nbsp;${img}<span class="bitem-name">${esc(b.name)}</span>` +
        (b.tag ? `<span class="btag">${esc(b.tag)}</span>` : '') + `</div>`);
    }
    return rows.join('');
  }

  // Enemy-laner and matchup result lines are tagged with the crossed-swords
  // glyph in the backend log text; we anchor-scroll to those when they appear.
  const isAnchorLog = (msg) => !!msg && msg.indexOf('⚔') !== -1;

  function renderLog() {
    const box = $('logBox');
    if (!box) return;
    // Was the user parked at the bottom before this re-render? (sticky bottom)
    const pinned = box.scrollHeight - box.scrollTop - box.clientHeight < 16;
    let lastAnchor = -1, anchorCount = 0;
    state.log.forEach((l, i) => { if (isAnchorLog(l.msg)) { lastAnchor = i; anchorCount++; } });
    box.innerHTML = state.log.map((l, i) =>
      `<div data-li="${i}"><span class="ts">${esc(l.ts)}</span>&nbsp; <span class="${l.cls}">${esc(l.msg)}</span></div>`).join('');
    if (anchorCount > (state._logAnchorCount || 0) && lastAnchor >= 0) {
      // A new enemy laner / matchup just locked in — bring it into view.
      const el = box.querySelector(`[data-li="${lastAnchor}"]`);
      if (el && el.scrollIntoView) el.scrollIntoView({ block: 'nearest' });
      else box.scrollTop = box.scrollHeight;
    } else if (pinned) {
      box.scrollTop = box.scrollHeight;
    }
    state._logAnchorCount = anchorCount;
  }

  function renderBuilds() {
    const count = state.builds.length;
    $('buildsCount').innerHTML =
      `<span class="classic-copy">${count} champions · everyone else follows u.gg</span>` +
      `<span class="standard-copy">${count} custom ${count === 1 ? 'build' : 'builds'}</span>`;
    const rows = $('ledgerRows');
    if (!state.builds.length) {
      rows.innerHTML = `<div class="ledger-empty"><span class="classic-copy">no custom builds yet — press [a] to add one</span>` +
        `<span class="standard-copy">No custom builds yet. Add one to override the recommended setup.</span></div>`;
      return;
    }
    rows.innerHTML = state.builds.map((b, i) => {
      const spellIds = [Number(b.spell1) || 0, Number(b.spell2) || 0].filter(Boolean);
      const spellNames = spellIds.map(SPELL_NAME);
      const spellIcons = spellIds.map(id =>
        `<img class="spell-icon" src="${spellIconUrl(id)}" alt="" title="${esc(SPELL_NAME(id))}">`
      ).join('');
      const spells = spellIds.length
        ? `<span class="spell-icons" role="img" aria-label="${esc(spellNames.join(' and '))}">${spellIcons}</span>` +
          `<span class="spell-names">${esc(b.summoners)}</span>`
        : `<span class="spell-names">${esc(b.summoners)}</span>` +
          `<span class="spell-default standard-copy">recommended</span>`;
      return (
      `<div class="ledger-row${i === state.sel ? ' sel' : ''}" data-idx="${i}">` +
      `<span class="num">${String(i + 1).padStart(2, '0')}</span>` +
      `<span class="ledger-champion">${esc(b.champ)}</span>` +
      `<span><span class="classic-copy">${esc(b.role)}</span><span class="standard-copy">${esc(titleCase(b.role))}</span></span>` +
      `<span class="sm">${esc(b.path)}</span><span class="sm ledger-spells">${spells}</span></div>`);
    }).join('');
  }

  function renderSettings() {
    const s = state.settings;
    $('setRank').textContent = s.rank + ' ▾';
    $('setRegion').textContent = s.region + ' ▾';
    $('setInterface').textContent = (s.interface_style === 'classic' ? 'DAEMON Classic' : 'Standard') + ' ▾';
    $('setPhosphor').textContent = (standardInterface() ? titleCase(s.phosphor) : s.phosphor) + ' ▾';
    [
      [$('setAutoRole'), !!s.auto_role],
      [$('setScoreV2Beta'), !!s.score_v2_beta],
      [$('setAutostart'), !!s.autostart]
    ].forEach(([el, enabled]) => {
      el.classList.toggle('on', enabled);
      el.setAttribute('aria-checked', String(enabled));
      const classic = el.querySelector('.classic-toggle');
      if (classic) classic.textContent = enabled ? '[x]' : '[ ]';
    });
    const betaSources = Array.isArray(s.score_v2_beta_sources)
      ? s.score_v2_beta_sources : [];
    const betaStatus = $('scoreV2BetaStatus');
    if (s.score_v2_beta_error) {
      betaStatus.textContent = '# model rejected; v1 fallback active';
    } else if (betaSources.length) {
      betaStatus.textContent = '# loaded: ' + betaSources.join(', ').replaceAll('_', ' ');
    } else if (s.score_v2_beta) {
      betaStatus.textContent = '# on by default; no local model installed, v1 active';
    } else {
      betaStatus.textContent = '# disabled; DAEMON Score v1 active';
    }
    document.querySelectorAll('[data-trig]').forEach(el => {
      const selected = el.dataset.trig === s.trigger;
      el.classList.toggle('selected', selected);
      el.setAttribute('aria-checked', String(selected));
      const classic = el.querySelector('.classic-radio');
      if (classic) classic.textContent = (selected ? '(•)' : '( )') +
        ' ' + (el.dataset.trig === 'hover' ? 'hover' : 'lock-in');
    });
  }

  function rateText(group) {
    return group && group.win_rate != null ? `${group.win_rate.toFixed(1)}%` : '—';
  }
  function recordText(group) {
    if (!group || !group.games) return 'no scored games';
    return `${group.wins}W · ${group.games - group.wins}L · ${group.games} games`;
  }
  function renderBreakdown(rows) {
    if (!rows || !rows.length) return '<div class="h-empty">no data</div>';
    return rows.slice(0, 4).map(row =>
      `<div class="hbreak-row"><span>${esc(row.name)}</span>` +
      `<span>${row.win_rate.toFixed(1)}% <i>${row.games}g</i></span></div>`
    ).join('');
  }
  function rankClass(rank) {
    if (rank === 1) return 'rank-first';
    if (rank <= 3) return 'rank-podium';
    if (rank <= 5) return 'rank-upper';
    return 'rank-lower';
  }
  function rankLabel(rank) {
    if (rank === 1) return 'MVP';
    if (rank === 2) return 'ELITE';
    if (rank === 3) return 'PODIUM';
    if (rank <= 5) return 'UPPER HALF';
    if (rank <= 8) return 'MID PACK';
    return 'ROUGH GAME';
  }
  function scoreBand(score) {
    if (score >= 80) return 'S-TIER';
    if (score >= 65) return 'A-TIER';
    if (score >= 50) return 'B-TIER';
    if (score >= 35) return 'C-TIER';
    return 'D-TIER';
  }
  const EVIDENCE_SOURCES = {
    match_v5: { label: 'Full timeline', detail: 'Riot Match-V5 timeline', cls: 'full' },
    lcu_timeline: { label: 'Local timeline', detail: 'Post-game League client timeline', cls: 'local' },
    live_client: { label: 'Live capture', detail: 'Reconciled local in-game capture', cls: 'live' },
    aggregate: { label: 'Aggregate estimate', detail: 'Post-game totals only', cls: 'aggregate' },
    aggregate_legacy: { label: 'Legacy aggregate', detail: 'DAEMON v1 post-game totals', cls: 'legacy' }
  };
  function evidenceSource(source) {
    return EVIDENCE_SOURCES[source] || {
      label: source ? plainMeta(String(source).replaceAll('_', ' ')) : 'Unknown evidence',
      detail: source || 'source unavailable',
      cls: 'unknown'
    };
  }
  function percentText(value) {
    if (value == null || value === '') return 'Not calibrated';
    const number = Number(value);
    return Number.isFinite(number) ? `${Math.round(number * 100)}%` : 'Not calibrated';
  }
  function confidenceText(value) {
    if (value == null || value === '') return 'Legacy scoring';
    const number = Number(value);
    if (!Number.isFinite(number)) return 'Legacy scoring';
    if (number >= 0.8) return 'High confidence';
    if (number >= 0.65) return 'Moderate confidence';
    return 'Limited confidence';
  }
  function intervalText(row) {
    if (row.score_low == null || row.score_high == null) return 'Not calibrated';
    const low = Number(row.score_low);
    const high = Number(row.score_high);
    return Number.isFinite(low) && Number.isFinite(high)
      ? `${low.toFixed(1)} - ${high.toFixed(1)}`
      : 'Not calibrated';
  }
  function readableReason(reason) {
    return titleCase(String(reason || 'insufficient evidence').replaceAll('_', ' '));
  }
  function abstainText(row) {
    const reasons = row.abstain_reasons || [];
    const detail = reasons.length
      ? reasons.map(readableReason).join(', ')
      : 'Insufficient evidence';
    return `Score withheld: ${detail}`;
  }
  function rankOrderLabel(row) {
    if (row.rank_confidence == null || row.rank_confidence === '') {
      return rankLabel(Number(row.match_rank));
    }
    const confidence = Number(row.rank_confidence);
    if (!Number.isFinite(confidence)) return rankLabel(Number(row.match_rank));
    return confidence < 0.7 ? 'CLOSE RANKING' : rankLabel(Number(row.match_rank));
  }
  function shortHash(value) {
    const text = String(value || '');
    return text ? text.slice(0, 10) : '';
  }
  function queueLabel(queueId) {
    return ({
      400: 'DRAFT', 420: 'RANKED SOLO', 430: 'BLIND',
      440: 'RANKED FLEX', 480: 'SWIFTPLAY', 490: 'QUICKPLAY'
    })[queueId] || `QUEUE ${queueId}`;
  }
  function historyWhen(value) {
    const d = new Date(value);
    return Number.isNaN(d.getTime()) ? '' : d.toLocaleDateString([], { month: 'short', day: 'numeric' });
  }
  function renderHistory() {
    const h = state.history;
    $('historyState').textContent = h.syncing ? 'syncing league data…' : 'local archive';
    $('historyError').hidden = !h.error;
    $('historyError').textContent = h.error || '';
    const summary = h.summary || {};
    $('historyOverallWr').textContent = rateText(summary.overall);
    $('historyOverallMeta').textContent = recordText(summary.overall);
    $('historyRecentWr').textContent = rateText(summary.recent20);
    $('historyRecentMeta').textContent = recordText(summary.recent20);
    $('historyChampions').innerHTML = renderBreakdown(summary.champions);
    $('historyRoles').innerHTML = renderBreakdown(summary.roles);
    const total = Number((summary.overall || {}).games || 0);
    const performance = summary.performance || {};
    $('historyArchiveCount').textContent = total;
    $('historyBestRank').textContent = performance.best_rank ? `#${performance.best_rank}` : '#—';
    $('historyAverageScore').textContent = performance.average_score == null
      ? '—'
      : Number(performance.average_score).toFixed(1);
    $('historyAverageRank').textContent = performance.average_rank == null
      ? '#—'
      : `#${Number(performance.average_rank).toFixed(1)}`;
    $('historyLoaded').innerHTML =
      `<span class="classic-copy">${h.rows.length} / ${total}</span>` +
      `<span class="standard-copy">Showing ${h.rows.length} of ${total}</span>`;
    const formRows = h.rows.slice(0, 10);
    $('historyRecentForm').setAttribute(
      'aria-label',
      formRows.length ? formRows.map(row => row.local_win ? 'win' : 'loss').join(', ') : 'No recent matches'
    );
    $('historyRecentForm').innerHTML = formRows.map(row =>
      `<i class="${row.local_win ? 'win' : 'loss'}" title="${row.local_win ? 'Victory' : 'Defeat'}"></i>`
    ).join('');
    $('historyFeed').setAttribute('aria-busy', String(h.loading || h.syncing));
    const rows = $('historyRows');
    if (!h.rows.length) {
      rows.innerHTML = `<div class="history-empty">${h.syncing ? 'syncing match history…' : 'no scored Summoner’s Rift games yet'}</div>`;
    } else {
      rows.innerHTML = h.rows.map(row => {
        const win = !!row.local_win;
        const score = Number(row.total_score);
        const rank = Number(row.match_rank);
        const duration = Math.max(0, Number(row.duration) || 0);
        const minutes = Math.floor(duration / 60);
        const seconds = String(duration % 60).padStart(2, '0');
        const result = win ? 'Victory' : 'Defeat';
        const deaths = Number(row.deaths) || 0;
        const kdaRatio = deaths
          ? ((Number(row.kills) + Number(row.assists)) / deaths).toFixed(2)
          : 'Perfect';
        const source = evidenceSource(row.evidence_source);
        const scoreState = row.abstain
          ? abstainText(row)
          : confidenceText(row.participant_confidence);
        const range = intervalText(row);
        const cardLabel = `${result}, ${row.local_champion_name}, ${row.local_role}, ${source.label}, DAEMON score ${score.toFixed(1)}, ${range} range, match rank ${rank} of 10, ${scoreState}`;
        return `<article class="history-card ${win ? 'is-win' : 'is-loss'}" data-game="${row.game_id}" ` +
          `role="button" tabindex="0" aria-label="${esc(cardLabel)}">` +
          `<div class="history-result"><strong class="classic-result-mark">${win ? 'W' : 'L'}</strong><span>${result}</span></div>` +
          `<div class="history-match">` +
            `<div class="history-champion-art"><span>${esc(String(row.local_champion_name || '?').slice(0, 2))}</span>` +
              `<img src="${championIconUrl(row.local_champion_id)}" alt="" loading="lazy" onerror="this.style.display='none'"></div>` +
            `<div class="history-match-copy">` +
              `<div class="history-match-title"><strong>${esc(row.local_champion_name)}</strong>` +
                `<span><span class="classic-copy">${esc(row.local_role)}</span>` +
                  `<span class="standard-copy">${esc(titleCase(row.local_role))}</span></span></div>` +
              `<div class="history-kda"><b><span>${row.kills}</span>/<span class="deaths">${row.deaths}</span>/<span>${row.assists}</span></b>` +
                `<span>KDA</span><em>${kdaRatio === 'Perfect' ? 'Perfect KDA' : `${kdaRatio} ratio`}</em></div>` +
              `<div class="history-match-meta">${titleCase(queueLabel(row.queue_id).toLowerCase())}<i>·</i>${minutes}:${seconds}<i>·</i>${historyWhen(row.game_creation_date)}</div>` +
              `<div class="history-evidence"><span class="evidence-badge ${source.cls}">${esc(source.label)}</span>` +
                `<span class="${row.abstain ? 'withheld' : ''}">${esc(scoreState)}</span></div>` +
            `</div>` +
          `</div>` +
          `<div class="history-score"><span><span class="classic-copy">DAEMON</span><span class="standard-copy">DAEMON score</span></span>` +
            `<strong>${score.toFixed(1)}</strong><em>${row.abstain ? 'WITHHELD' : scoreBand(score)}</em>` +
            `<small>${esc(range)}</small></div>` +
          `<div class="history-rank ${rankClass(rank)}"><span>Match rank</span><strong>#${rank}</strong><em>${rankOrderLabel(row)}</em></div>` +
        `</article>`;
      }).join('');
    }
    h.hasMore = h.rows.length < total;
    const more = $('historyMore');
    const moreDisabled = !h.hasMore || h.loading;
    more.classList.toggle('disabled', moreDisabled);
    more.setAttribute('aria-disabled', String(moreDisabled));
    more.tabIndex = moreDisabled ? -1 : 0;
    const remaining = Math.min(12, total - h.rows.length);
    more.innerHTML = h.loading
      ? '<span class="classic-copy">[loading archive...]</span><span class="standard-copy">Loading matches…</span>'
      : h.hasMore
        ? `<span class="classic-copy">[load ${remaining} more]</span><span class="standard-copy">Load ${remaining} more</span>`
        : `<span class="classic-copy">[archive complete // ${h.rows.length} matches]</span><span class="standard-copy">All ${h.rows.length} matches loaded</span>`;
  }
  function loadHistory(reset) {
    if (!window.API.ready()) { renderHistory(); return; }
    if (state.history.loading) return;
    if (reset) {
      state.history.offset = 0; state.history.rows = []; state.history.hasMore = true;
    } else if (!state.history.hasMore) {
      return;
    }
    state.history.loading = true;
    renderHistory();
    const offset = state.history.offset;
    Promise.all([
      window.API.call('get_history_summary'),
      window.API.call('get_match_history', offset, 12)
    ]).then(([summary, rows]) => {
      state.history.summary = summary || state.history.summary;
      const incoming = rows || [];
      const known = new Set(reset ? [] : state.history.rows.map(row => row.game_id));
      const unique = incoming.filter(row => !known.has(row.game_id));
      state.history.rows = reset ? unique : state.history.rows.concat(unique);
      state.history.offset = state.history.rows.length;
      state.history.loaded = true;
      state.history.loading = false;
      state.history.error = '';
      renderHistory();
    }).catch(e => {
      state.history.loading = false;
      state.history.error = String(e); renderHistory();
    });
  }
  function renderReport() {
    const report = state.report;
    if (!report || !report.match || !report.participants) return;
    const match = report.match;
    const local = report.participants.find(p => p.participant_id === match.local_participant_id);
    if (!local) return;
    $('reportResult').textContent = local.win ? 'VICTORY' : 'DEFEAT';
    $('reportResult').className = 'report-result ' + (local.win ? 'win' : 'loss');
    $('reportHero').className = `report-hero ${local.win ? 'is-win' : 'is-loss'} ${rankClass(local.match_rank)}${local.abstain ? ' is-abstained' : ''}`;
    $('reportTitle').textContent = `${local.champion_name} · ${local.role}`;
    const minutes = Math.floor(match.duration / 60);
    const seconds = String(match.duration % 60).padStart(2, '0');
    $('reportMeta').textContent = `${local.kills}/${local.deaths}/${local.assists} · ${local.cs} CS · ${minutes}:${seconds} · ${match.patch}`;
    $('reportScore').textContent = Number(local.total_score).toFixed(1);
    $('reportScoreBand').textContent = local.abstain
      ? 'SCORE WITHHELD'
      : `${scoreBand(Number(local.total_score))} PERFORMANCE`;
    $('reportRank').textContent = `#${local.match_rank}`;
    $('reportRankLabel').textContent = `${rankOrderLabel(local)} // OF 10`;
    $('reportModel').textContent = local.model_version;
    const source = evidenceSource(local.evidence_source);
    $('reportEvidenceSource').textContent = source.label;
    const completeness = local.score_confidence
      && local.score_confidence.chosen_source_completeness;
    $('reportEvidenceDetail').textContent = completeness == null
      ? source.detail
      : `${source.detail} · ${percentText(completeness)} complete`;
    $('reportConfidence').textContent = confidenceText(local.participant_confidence);
    $('reportConfidenceDetail').textContent = local.abstain
      ? abstainText(local)
      : local.participant_confidence == null
        ? 'legacy estimate; no calibrated confidence'
        : `${percentText(local.participant_confidence)} participant confidence`;
    $('reportInterval').textContent = intervalText(local);
    $('reportRankConfidence').textContent = percentText(local.rank_confidence);
    $('reportRankDetail').textContent = local.rank_confidence == null
      ? 'legacy rank order; no confidence estimate'
      : Number(local.rank_confidence) < 0.7
        ? 'close ordering; treat nearby ranks as tied'
        : 'estimated ordering stability';
    const artifact = local.artifact_model_version || '';
    const artifactHash = shortHash(local.model_artifact_hash);
    const provenance = artifact
      ? `${local.model_family || 'model'} · artifact ${artifact}${artifactHash ? ` · ${artifactHash}` : ''}`
      : 'legacy role-weighted model';
    const calibration = local.calibration_version
      ? ` Calibration: ${local.calibration_version}.`
      : '';
    $('reportMethodDetail').textContent =
      ` Evidence: ${source.detail}. Provenance: ${provenance}.${calibration}`;
    const labels = {
      combat: 'combat', economy: 'economy', objectives: 'objectives',
      vision: 'vision', teamplay: 'survival/teamplay'
    };
    const components = Object.entries(local.components || {});
    $('reportComponents').innerHTML = components.length
      ? components.map(([key, value]) => {
        const amount = Number(value);
        const safeAmount = Number.isFinite(amount) ? amount : 0;
        return `<div class="component-row"><span>${esc(labels[key] || readableReason(key))}</span>` +
          `<div class="component-track"><i style="width:${Math.max(0, Math.min(100, safeAmount))}%"></i></div>` +
          `<b>${safeAmount.toFixed(1)}</b></div>`;
      }).join('')
      : '<div class="component-empty">Score v2 replaces legacy category bars with source-specific evidence, confidence, and calibrated intervals.</div>';
    $('reportCoaching').innerHTML = renderCoaching(local.coaching || {}, local);
    const localTeam = local.team_id;
    let previousTeam = null;
    $('reportPlayers').innerHTML = report.participants.map(player => {
      const teamHeader = player.team_id !== previousTeam
        ? `<div class="report-team">${player.team_id === localTeam ? 'YOUR TEAM' : 'ENEMY TEAM'}</div>`
        : '';
      previousTeam = player.team_id;
      const rank = Number(player.match_rank);
      const playerInterval = intervalText(player);
      const playerScoreState = player.abstain
        ? abstainText(player)
        : `${confidenceText(player.participant_confidence)}; range ${playerInterval}`;
      return teamHeader +
        `<div class="report-player ${rankClass(rank)}${player.participant_id === match.local_participant_id ? ' local' : ''}${player.abstain ? ' is-abstained' : ''}">` +
        `<span class="player-rank">#${rank}<i>${rankOrderLabel(player)}</i></span><span>${esc(player.summoner_name)}</span>` +
        `<span>${esc(player.champion_name)}</span><span>${esc(player.role)}</span>` +
        `<span>${player.kills}/${player.deaths}/${player.assists}</span>` +
        `<span class="player-score" title="${esc(playerScoreState)}">${Number(player.total_score).toFixed(1)}</span></div>`;
    }).join('');
  }
  function renderCoaching(coaching, local) {
    if (coaching.eligible && coaching.primary_focus) {
      const challenge = (coaching.challenges || [])[0] || {};
      const pattern = (coaching.recurring_patterns || []).find(
        row => row.title === coaching.primary_focus
      ) || (coaching.recurring_patterns || [])[0] || {};
      return `<div class="coaching-head"><span class="coaching-state eligible">PRIMARY FOCUS</span>` +
        `<span>${Number(pattern.occurrences || 0)} of ${Number(pattern.games_considered || 0)} comparable games</span></div>` +
        `<h3>${esc(coaching.primary_focus)}</h3>` +
        `${pattern.current_evidence ? `<p>${esc(pattern.current_evidence)}</p>` : ''}` +
        `<div class="coaching-challenge"><span>3 OF 5 CHALLENGE</span>` +
          `<strong>${esc(challenge.target || 'Challenge target unavailable.')}</strong>` +
          `<small>${esc(challenge.measurement || '')}</small></div>` +
        `<div class="coaching-guardrail"><b>Guardrail</b><span>${esc(challenge.anti_gaming_guardrail || '')}</span></div>`;
    }
    let reasons = coaching.withheld_reasons || [];
    if (!reasons.length && Number(local.model_version) < 2) {
      reasons = ['Evidence-backed coaching begins with DAEMON Score v2.'];
    }
    if (!reasons.length) {
      reasons = ['No recurring controllable pattern met the coaching threshold.'];
    }
    return `<div class="coaching-head"><span class="coaching-state withheld">COACHING WITHHELD</span>` +
      '<span>Evidence gate</span></div>' +
      '<h3>No focus assigned from this match</h3>' +
      `<ul>${reasons.map(reason => `<li>${esc(reason)}</li>`).join('')}</ul>`;
  }
  function openReport(gameId) {
    if (!window.API.ready()) return;
    window.API.call('get_match_report', gameId).then(report => {
      if (!report || !report.match) return;
      state.report = report;
      setScreen('report');
      renderReport();
    });
  }

  function renderOverlay() {
    $('overlay').hidden = !state.inGame;
    if (!state.inGame) return;
    $('ovMatch').innerHTML = `${esc(state.champ || '—')} <span class="vs">vs</span> ${esc(state.enemy || '—')}`;
    const down = state.wr != null && state.wr < 50;
    $('ovWr').innerHTML = state.wr == null
      ? '—'
      : `${state.wr.toFixed(1)}%  <span class="arr">${down ? '▼' : '▲'}</span>`;
    $('ovCtx').textContent = state.wrLabel
      ? state.wrLabel.toLowerCase()
      : (state.enemy ? 'win rate unavailable' : 'awaiting matchup');
  }

  function setScreen(name) {
    if (!PROMPTS[name]) return;
    state.screen = name;
    document.querySelectorAll('[data-view]').forEach(v => { v.hidden = v.dataset.view !== name; });
    document.querySelectorAll('.tab').forEach(t => {
      const active = t.dataset.screen === name || (name === 'report' && t.dataset.screen === 'history');
      t.classList.toggle('active', active);
      if (active) t.setAttribute('aria-current', 'page');
      else t.removeAttribute('aria-current');
    });
    renderPrompt();
    if (name === 'history') {
      setHistorySection(state.history.section);
      if (!state.history.loaded) loadHistory(true);
    }
    const body = $('viewBody');
    if (body) {
      body.dataset.screen = name;
      if (body.focus) body.focus({ preventScroll: true });
    }
  }

  function applyTheme(name) {
    document.documentElement.setAttribute('data-phosphor', name);
    state.settings.phosphor = name;
    renderSettings();
  }
  function applyInterface(name) {
    const interfaceStyle = name === 'classic' ? 'classic' : 'standard';
    document.documentElement.setAttribute('data-interface', interfaceStyle);
    state.settings.interface_style = interfaceStyle;
    renderPrompt();
    renderSettings();
    renderMonitor();
    renderBuilds();
    renderHistory();
  }
  function renderPrompt() {
    const hint = $('promptHint');
    if (!hint) return;
    const standard = document.documentElement.getAttribute('data-interface') === 'standard';
    hint.textContent = (standard ? STANDARD_PROMPTS : PROMPTS)[state.screen] || '';
  }

  function renderAll() {
    renderStatus(); renderMonitor(); renderBuilds(); renderSettings(); renderHistory(); renderOverlay();
    if (state.report) renderReport();
  }

  // ── commands ────────────────────────────────────────────────────────────
  function toggleMonitoring() {
    if (window.API.ready()) {
      // backend confirms via the 'running' push — don't flip optimistically,
      // so a failed start (not connected) doesn't leave a wrong label.
      window.API.call(state.monitoring ? 'stop_monitoring' : 'start_monitoring');
    } else {
      state.monitoring = !state.monitoring;
    }
  }
  function toggleOverlay() { state.inGame = !state.inGame; renderOverlay(); }
  function cmd(action) {
    if (action === 'toggle') toggleMonitoring();
    else if (action === 'overlay') toggleOverlay();
    else if (action === 'tray') window.API.call('hide_to_tray');
    else if (action === 'reimport') window.API.call('reimport');
    else if (action === 'clear') { state.log = []; renderMonitor(); }
    else if (action === 'add') openEditor('', true);
    else if (action === 'edit') { const c = state.builds[state.sel]; if (c) openEditor(c.champ, false); }
    else if (action === 'delete') { const c = state.builds[state.sel]; if (c) confirmDelete(c.champ); }
  }

  function refreshBuilds() {
    if (!window.API.ready()) { renderBuilds(); return; }
    window.API.call('get_builds').then(b => {
      state.builds = b || [];
      if (state.sel >= state.builds.length) state.sel = Math.max(0, state.builds.length - 1);
      renderBuilds();
    });
  }

  function confirmDelete(champ) {
    if (!window.confirm(`Remove custom build for ${champ}?`)) return;
    window.API.call('remove_override', champ).then(() => refreshBuilds());
  }
  function moveSel(d) {
    if (!state.builds.length) return;
    state.sel = Math.max(0, Math.min(state.builds.length - 1, state.sel + d));
    renderBuilds();
  }

  // ── wiring ────────────────────────────────────────────────────────────────
  function typing(e) {
    const t = e.target;
    return t && (t.tagName === 'INPUT' || t.tagName === 'TEXTAREA' || t.isContentEditable);
  }
  function activateButtonOnKey(e) {
    if (e.key !== 'Enter' && e.key !== ' ') return;
    e.preventDefault();
    e.currentTarget.click();
  }
  function setHistorySection(section, focusTab) {
    const allowed = ['matches', 'overview', 'champions', 'roles'];
    const next = allowed.includes(section) ? section : 'matches';
    state.history.section = next;
    document.querySelectorAll('[data-history-section]').forEach(tab => {
      const active = tab.dataset.historySection === next;
      tab.classList.toggle('active', active);
      tab.setAttribute('aria-selected', String(active));
      tab.tabIndex = active ? 0 : -1;
      if (active && focusTab) tab.focus();
    });
    document.querySelectorAll('[data-history-panel]').forEach(panel => {
      panel.hidden = panel.dataset.historyPanel !== next;
    });
  }
  function onHistoryTabKey(e) {
    const tabs = Array.from(document.querySelectorAll('[data-history-section]'));
    const current = tabs.indexOf(e.currentTarget);
    let next = current;
    if (e.key === 'ArrowRight') next = (current + 1) % tabs.length;
    else if (e.key === 'ArrowLeft') next = (current - 1 + tabs.length) % tabs.length;
    else if (e.key === 'Home') next = 0;
    else if (e.key === 'End') next = tabs.length - 1;
    else return;
    e.preventDefault();
    setHistorySection(tabs[next].dataset.historySection, true);
  }
  function onKey(e) {
    const k = (e.key || '').toLowerCase();
    if (k === 'escape' && state.screen === 'builder') { setScreen('editor'); return; }
    if (k === 'escape' && state.screen === 'editor') { setScreen('builds'); return; }
    if (k === 'escape' && state.screen === 'report') { setScreen('history'); return; }
    if (e.ctrlKey || e.altKey || e.metaKey || typing(e)) return;
    if (k === '1') setScreen('monitor');
    else if (k === '2') setScreen('builds');
    else if (k === '3') setScreen('settings');
    else if (k === '4') setScreen('history');
    else if (k === 's') cmd('toggle');
    else if (k === 'g') cmd('overlay');
    else if (k === 'q') cmd('tray');
    else if (state.screen === 'builds') {
      if (k === 'arrowup') { e.preventDefault(); moveSel(-1); }
      else if (k === 'arrowdown') { e.preventDefault(); moveSel(1); }
      else if (k === 'a') cmd('add');
      else if (k === 'e') cmd('edit');
      else if (k === 'd') cmd('delete');
    }
  }

  function wire() {
    // The whole .topbar is the window drag handle (pywebview-drag-region), but
    // the interactive controls inside it must act like buttons, not drag the
    // window. Swallowing mousedown stops pywebview's drag from starting on them
    // while leaving their click handlers intact — normal title-bar behaviour.
    document.querySelectorAll('.topbar .tab, .topbar .status, .topbar .wbtn')
      .forEach(el => el.addEventListener('mousedown', e => e.stopPropagation()));
    document.querySelectorAll('.tab').forEach(t =>
      t.addEventListener('click', () => setScreen(t.dataset.screen)));
    document.querySelectorAll('[role="button"], [role="switch"], [role="radio"]').forEach(el =>
      el.addEventListener('keydown', activateButtonOnKey));
    document.querySelectorAll('[data-cmd]').forEach(el =>
      el.addEventListener('click', () => cmd(el.dataset.cmd)));
    $('status').addEventListener('click', toggleMonitoring);
    $('wMin').addEventListener('click', () => window.API.call('minimize'));
    $('wMax').addEventListener('click', () => window.API.call('toggle_fullscreen'));
    $('wClose').addEventListener('click', () => window.API.call('hide_to_tray'));
    document.querySelectorAll('.ledger-row, #ledgerRows').forEach(() => {});
    $('ledgerRows').addEventListener('click', (e) => {
      const row = e.target.closest('.ledger-row'); if (row) { state.sel = +row.dataset.idx; renderBuilds(); }
    });
    // settings (P1: client-side cycling/toggles; P4 wires to backend + menus)
    $('setRank').addEventListener('click', () =>
      openMenu($('setRank'), RANKS, state.settings.rank, v => { state.settings.rank = v; renderSettings(); }));
    $('setRegion').addEventListener('click', () =>
      openMenu($('setRegion'), REGIONS, state.settings.region, v => { state.settings.region = v; renderSettings(); }));
    $('setInterface').addEventListener('click', () =>
      openMenu($('setInterface'), INTERFACES, state.settings.interface_style, v => {
        applyInterface(v);
        window.API.call('set_interface', v);
      }));
    $('setPhosphor').addEventListener('click', () =>
      openMenu($('setPhosphor'), PHOSPHORS, state.settings.phosphor, v => {
        applyTheme(v); window.API.call('set_theme', v);   // theme persists immediately
      }));
    $('setAutoRole').addEventListener('click', () => { state.settings.auto_role = !state.settings.auto_role; renderSettings(); });
    $('setScoreV2Beta').addEventListener('click', () => {
      state.settings.score_v2_beta = !state.settings.score_v2_beta;
      renderSettings();
    });
    $('setAutostart').addEventListener('click', () => {
      const next = !state.settings.autostart;
      if (window.API.ready()) {
        window.API.call('set_autostart', next).then(r => {
          state.settings.autostart = r ? !!r.enabled : state.settings.autostart; renderSettings();
        });
      } else { state.settings.autostart = next; renderSettings(); }
    });
    document.querySelectorAll('[data-trig]').forEach(el =>
      el.addEventListener('click', () => { state.settings.trigger = el.dataset.trig; renderSettings(); }));
    $('saveBtn').addEventListener('click', () => {
      window.API.call('save_settings', JSON.parse(JSON.stringify(state.settings)));
      const m = $('saveMsg'); m.hidden = false; clearTimeout(m._t); m._t = setTimeout(() => { m.hidden = true; }, 2600);
    });
    $('moGo').addEventListener('click', submitMatchup);
    $('moInput').addEventListener('keydown', (e) => { if (e.key === 'Enter') submitMatchup(); });
    $('historyRefresh').addEventListener('click', () => {
      state.history.syncing = true; renderHistory();
      window.API.call('refresh_match_history').then(result => {
        if (!result || !result.ok) {
          state.history.syncing = false;
          state.history.error = (result && result.error) || 'League not connected.';
          renderHistory();
        }
      });
    });
    document.querySelectorAll('[data-history-section]').forEach(tab => {
      tab.addEventListener('click', () => setHistorySection(tab.dataset.historySection));
      tab.addEventListener('keydown', onHistoryTabKey);
    });
    $('historyMore').addEventListener('click', () => loadHistory(false));
    $('historyFeed').addEventListener('scroll', () => {
      const feed = $('historyFeed');
      if (feed.scrollHeight - feed.scrollTop - feed.clientHeight < 80) loadHistory(false);
    });
    $('historyRows').addEventListener('click', e => {
      const row = e.target.closest('.history-card');
      if (row) openReport(Number(row.dataset.game));
    });
    $('historyRows').addEventListener('keydown', e => {
      if (e.key !== 'Enter' && e.key !== ' ') return;
      const row = e.target.closest('.history-card');
      if (!row) return;
      e.preventDefault();
      openReport(Number(row.dataset.game));
    });
    $('reportBack').addEventListener('click', () => setScreen('history'));
    wireEditor();
    wireBuilder();
    wireDebug();
    window.addEventListener('keydown', onKey);
  }
  function submitMatchup() {
    const v = $('moInput').value.trim(); if (!v) return;
    $('moInput').value = '';
    window.API.call('set_matchup_override', v);
  }

  // ── override editor ─────────────────────────────────────────────────────────
  const ed = { champ: '', role: 'auto', primary_tree: 'Precision', keystone: '',
               secondary_tree: 'Domination', rune_ids: [], note: '', page_name: '',
               spell1: 0, spell2: 0, items_build: {} };
  let edNew = true;

  function openEditor(champ, isNew) {
    edNew = isNew;
    const fill = (o) => {
      Object.assign(ed, {
        champ: isNew ? '' : (o.champ || champ || ''),
        role: o.role || 'auto', primary_tree: o.primary_tree || 'Precision',
        keystone: o.keystone || '', secondary_tree: o.secondary_tree || 'Domination',
        rune_ids: o.rune_ids || [], note: o.note || '', page_name: o.page_name || '',
        spell1: o.spell1 || 0, spell2: o.spell2 || 0, items_build: o.items_build || {}
      });
      renderEditor(); setScreen('editor'); $('edChamp').focus();
    };
    if (window.API.ready()) window.API.call('get_override', champ || '').then(o => fill(o || {}));
    else fill({ champ: champ || '' });
  }

  function renderEditor() {
    $('edTitle').textContent = edNew ? '~/add override' : '~/edit override';
    $('edChamp').value = ed.champ || '';
    $('edRole').textContent = (ed.role || 'auto') + ' ▾';
    $('edPrimary').textContent = (ed.primary_tree || 'Precision') + ' ▾';
    $('edKeystone').textContent = (ed.keystone || '—') + ' ▾';
    $('edSecondary').textContent = (ed.secondary_tree || 'Domination') + ' ▾';
    $('edSpell1').textContent = SPELL_NAME(ed.spell1) + ' ▾';
    $('edSpell2').textContent = SPELL_NAME(ed.spell2) + ' ▾';
    $('edRunes').value = (ed.rune_ids || []).join(',');
    $('edNote').value = ed.note || '';
    const ni = Object.values(ed.items_build || {}).reduce((a, v) => a + (Array.isArray(v) ? v.length : 0), 0);
    $('edBuild').textContent = ni ? `[ edit build · ${ni} items ]` : '[ edit build ]';
    $('edStatus').textContent = '';
  }

  function saveEditor() {
    ed.champ = $('edChamp').value.trim();
    ed.note = $('edNote').value;
    const runesStr = $('edRunes').value.trim();   // string; backend parses/validates
    if (!ed.champ) { $('edStatus').textContent = '✗ enter a champion name'; return; }
    if (!window.API.ready()) { setScreen('builds'); return; }
    window.API.call('save_override', ed.champ, {
      role: ed.role, primary_tree: ed.primary_tree, keystone: ed.keystone,
      secondary_tree: ed.secondary_tree, rune_ids: runesStr, note: ed.note,
      page_name: ed.page_name, spell1: ed.spell1, spell2: ed.spell2, items_build: ed.items_build
    }).then(r => {
      if (r && r.ok) { refreshBuilds(); setScreen('builds'); }
      else $('edStatus').textContent = '✗ ' + ((r && r.error) || 'save failed');
    });
  }

  function importFromClient() {
    $('edStatus').textContent = '… reading client';
    window.API.call('import_rune_page_from_client').then(r => {
      if (r && r.ok) {
        ed.primary_tree = r.primary_tree || ed.primary_tree;
        ed.secondary_tree = r.secondary_tree || ed.secondary_tree;
        if (r.keystone) ed.keystone = r.keystone;
        ed.rune_ids = r.rune_ids || []; ed.page_name = r.page_name || '';
        renderEditor();
        $('edStatus').textContent = '✓ imported ' + (r.page_name || 'page');
      } else $('edStatus').textContent = '✗ ' + ((r && r.error) || 'failed');
    });
  }

  // floating dropdown (editor + future settings menus)
  let _menuEl = null, _menuAnchor = null;
  function closeMenu(restoreFocus) {
    if (_menuEl) {
      const anchor = _menuAnchor;
      _menuEl.remove(); _menuEl = null;
      if (anchor) anchor.setAttribute('aria-expanded', 'false');
      _menuAnchor = null;
      document.removeEventListener('mousedown', _menuOutside, true);
      if (restoreFocus && anchor && anchor.focus) anchor.focus();
    }
  }
  function _menuOutside(e) { if (_menuEl && !_menuEl.contains(e.target)) closeMenu(); }
  function openMenu(anchor, options, current, onPick) {
    closeMenu();
    const m = document.createElement('div'); m.className = 'menu-pop'; m.setAttribute('role', 'menu');
    anchor.setAttribute('aria-expanded', 'true');
    _menuAnchor = anchor;
    const items = [];
    const focusItem = index => {
      const next = (index + items.length) % items.length;
      items.forEach((item, i) => { item.tabIndex = i === next ? 0 : -1; });
      items[next].focus();
    };
    options.forEach(opt => {
      const label = Array.isArray(opt) ? opt[0] : opt;
      const value = Array.isArray(opt) ? opt[1] : opt;
      const it = document.createElement('div');
      const selected = value === current;
      it.className = 'mi' + (selected ? ' on' : '');
      it.setAttribute('role', 'menuitemradio');
      it.setAttribute('aria-checked', String(selected));
      const text = document.createElement('span'); text.textContent = label;
      const check = document.createElement('span'); check.className = 'mi-check'; check.textContent = '✓';
      it.append(text, check);
      it.tabIndex = selected ? 0 : -1;
      const pick = () => { onPick(value); closeMenu(true); };
      it.addEventListener('click', pick);
      it.addEventListener('keydown', e => {
        const index = items.indexOf(it);
        if (e.key === 'ArrowDown') { e.preventDefault(); focusItem(index + 1); }
        else if (e.key === 'ArrowUp') { e.preventDefault(); focusItem(index - 1); }
        else if (e.key === 'Home') { e.preventDefault(); focusItem(0); }
        else if (e.key === 'End') { e.preventDefault(); focusItem(items.length - 1); }
        else if (e.key === 'Escape') { e.preventDefault(); closeMenu(true); }
        else if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); pick(); }
      });
      items.push(it);
      m.appendChild(it);
    });
    document.body.appendChild(m);
    const r = anchor.getBoundingClientRect();
    m.style.left = r.left + 'px'; m.style.top = (r.bottom + 2) + 'px';
    setTimeout(() => document.addEventListener('mousedown', _menuOutside, true), 0);
    _menuEl = m;
    requestAnimationFrame(() => {
      const selected = items.findIndex(item => item.classList.contains('on'));
      focusItem(selected >= 0 ? selected : 0);
    });
  }

  function wireEditor() {
    $('edBack').addEventListener('click', () => setScreen('builds'));
    $('edCancel').addEventListener('click', () => setScreen('builds'));
    $('edSave').addEventListener('click', saveEditor);
    $('edImport').addEventListener('click', importFromClient);
    $('edRole').addEventListener('click', () => openMenu($('edRole'), ROLES, ed.role, v => { ed.role = v; renderEditor(); }));
    $('edPrimary').addEventListener('click', () => openMenu($('edPrimary'), TREES, ed.primary_tree, v => {
      ed.primary_tree = v; if (!(KEYSTONES[v] || []).includes(ed.keystone)) ed.keystone = ''; renderEditor();
    }));
    $('edKeystone').addEventListener('click', () => openMenu($('edKeystone'), KEYSTONES[ed.primary_tree] || [], ed.keystone, v => { ed.keystone = v; renderEditor(); }));
    $('edSecondary').addEventListener('click', () => openMenu($('edSecondary'), TREES, ed.secondary_tree, v => { ed.secondary_tree = v; renderEditor(); }));
    $('edSpell1').addEventListener('click', () => openMenu($('edSpell1'), SPELLS, ed.spell1, v => { ed.spell1 = v; renderEditor(); }));
    $('edSpell2').addEventListener('click', () => openMenu($('edSpell2'), SPELLS, ed.spell2, v => { ed.spell2 = v; renderEditor(); }));
    $('edBuild').addEventListener('click', openBuilder);
  }

  // ── item-build editor ───────────────────────────────────────────────────────
  const SLOTS = [['starter', 'STARTER'], ['core', 'CORE'], ['fourth', '4TH'], ['fifth', '5TH'], ['sixth', '6TH']];
  const SLOT_LABEL = k => (SLOTS.find(s => s[0] === k) || ['', '?'])[1];
  const bld = { starter: [], core: [], fourth: [], fifth: [], sixth: [], target: 'core' };
  let _blResults = [], _blDebounce = null;

  function openBuilder() {
    const ib = ed.items_build || {};
    SLOTS.forEach(([k]) => { bld[k] = (ib[k] || []).map(x => ({ id: x.id, name: x.name })); });
    bld.target = 'core';
    $('blTitle').textContent = '~/edit build · ' + (ed.champ || $('edChamp').value.trim() || 'champion');
    $('blQuery').value = ''; $('blResults').innerHTML = '';
    $('blTarget').textContent = SLOT_LABEL(bld.target) + ' ▾';
    renderBuilderSlots();
    setScreen('builder'); $('blQuery').focus();
  }

  function renderBuilderSlots() {
    $('blSlots').innerHTML = SLOTS.map(([k, label]) => {
      const items = bld[k] || [];
      const pills = items.length
        ? items.map((it, i) =>
            `<span class="bl-pill" data-slot="${k}" data-idx="${i}">` +
            (it.icon ? `<img src="${esc(it.icon)}" onerror="this.style.display='none'">` : '') +
            `${esc(it.name)} ✕</span>`).join('')
        : `<span class="bl-empty">—</span>`;
      return `<div class="bl-slot"><span class="bl-slot-k">${label}</span>${pills}</div>`;
    }).join('');
  }

  function renderResults(list) {
    _blResults = list || [];
    $('blResults').innerHTML = _blResults.map((it, i) =>
      `<span class="bl-result" data-i="${i}">` +
      (it.icon ? `<img src="${esc(it.icon)}" onerror="this.style.display='none'">` : '') +
      `${esc(it.name)}</span>`).join('');
  }

  function blSearch() {
    const q = $('blQuery').value.trim();
    clearTimeout(_blDebounce);
    if (!q) { $('blResults').innerHTML = ''; return; }
    _blDebounce = setTimeout(() => {
      if (window.API.ready()) window.API.call('search_items', q).then(renderResults);
    }, 250);
  }

  function saveBuilder() {
    const out = {};
    SLOTS.forEach(([k]) => { out[k] = bld[k].map(x => ({ id: x.id, name: x.name })); });
    ed.items_build = out;
    renderEditor();
    setScreen('editor');
  }

  function wireBuilder() {
    $('blBack').addEventListener('click', () => setScreen('editor'));
    $('blCancel').addEventListener('click', () => setScreen('editor'));
    $('blSave').addEventListener('click', saveBuilder);
    $('blQuery').addEventListener('input', blSearch);
    $('blTarget').addEventListener('click', () =>
      openMenu($('blTarget'), SLOTS.map(([k, l]) => [l, k]), bld.target,
        v => { bld.target = v; $('blTarget').textContent = SLOT_LABEL(v) + ' ▾'; }));
    $('blResults').addEventListener('click', e => {
      const r = e.target.closest('.bl-result'); if (!r) return;
      const it = _blResults[+r.dataset.i];
      if (it) { bld[bld.target].push({ id: it.id, name: it.name, icon: it.icon }); renderBuilderSlots(); }
    });
    $('blSlots').addEventListener('click', e => {
      const p = e.target.closest('.bl-pill'); if (!p) return;
      bld[p.dataset.slot].splice(+p.dataset.idx, 1); renderBuilderSlots();
    });
  }

  // ── debug console (Ctrl+Shift+D) ────────────────────────────────────────────
  const LVL = { debug: 0, info: 1, warn: 2, error: 3 };
  const TAGGABLE = new Set(['[ugg]', '[lcu]', '[monitor]', '[unknown]']);
  const dbg = { records: [], tags: new Set(TAGGABLE), minLevel: 'debug', prev: 'monitor' };

  function dbgPass(r) {
    // untracked tags ([app]/[crash]/[merge]/…) always show, so uncaught
    // exceptions can never be silently filtered out (matches the old console).
    const tagOk = TAGGABLE.has(r.tag) ? dbg.tags.has(r.tag) : true;
    return tagOk && (LVL[r.sev] || 0) >= (LVL[dbg.minLevel] || 0);
  }
  function dbgLine(r) {
    return `<div><span class="ts">${esc(r.ts)}</span>  <span class="tg">${esc((r.tag || '').padEnd(10))}</span>  ` +
           `<span class="sev-${esc(r.sev)}">${esc((r.sev || '').toUpperCase().padEnd(5))}  ${esc(r.msg)}</span></div>`;
  }
  function dbgCount() {
    $('dbgCount').textContent = `${dbg.records.filter(dbgPass).length} shown · ${dbg.records.length} total`;
  }
  function renderDebug() {
    $('dbgLog').innerHTML = dbg.records.filter(dbgPass).map(dbgLine).join('');
    $('dbgLog').scrollTop = $('dbgLog').scrollHeight;
    dbgCount();
  }
  function onLogrec(r) {
    dbg.records.push(r);
    if (dbg.records.length > 2000) dbg.records = dbg.records.slice(-1500);
    if (state.screen === 'debug' && dbgPass(r)) {
      $('dbgLog').insertAdjacentHTML('beforeend', dbgLine(r));
      $('dbgLog').scrollTop = $('dbgLog').scrollHeight;
      dbgCount();
    }
  }
  function toggleDebug() {
    if (state.screen === 'debug') setScreen(dbg.prev || 'monitor');
    else { dbg.prev = state.screen; setScreen('debug'); renderDebug(); }
  }
  function wireDebug() {
    document.querySelectorAll('.dbg-tag').forEach(el => el.addEventListener('click', () => {
      const t = el.dataset.tag;
      if (dbg.tags.has(t)) dbg.tags.delete(t); else dbg.tags.add(t);
      el.classList.toggle('on'); renderDebug();
    }));
    document.querySelectorAll('.dbg-lvl').forEach(el => el.addEventListener('click', () => {
      dbg.minLevel = el.dataset.lvl;
      document.querySelectorAll('.dbg-lvl').forEach(x => x.classList.toggle('on', x === el));
      renderDebug();
    }));
    $('dbgClear').addEventListener('click', () => { dbg.records = []; renderDebug(); });
    $('dbgCopy').addEventListener('click', () => {
      const text = dbg.records.filter(dbgPass)
        .map(r => `${r.ts}  ${r.tag}  ${(r.sev || '').toUpperCase()}  ${r.msg}`).join('\n');
      try { navigator.clipboard.writeText(text); }
      catch (e) {
        const ta = document.createElement('textarea'); ta.value = text;
        document.body.appendChild(ta); ta.select();
        try { document.execCommand('copy'); } catch (_) {} ta.remove();
      }
    });
    window.addEventListener('keydown', e => {
      if (e.ctrlKey && e.shiftKey && (e.key || '').toLowerCase() === 'd') { e.preventDefault(); toggleDebug(); }
    });
  }

  // ── Python → JS events (drained from poll_events by the poll loop) ──────────
  function pushLog(rec) {
    state.log.push({ ts: rec.ts, msg: rec.msg, cls: rec.cls || '' });
    if (state.log.length > 300) state.log = state.log.slice(-250);
  }
  function handlePush(event, p) {
    switch (event) {
      case 'status': state.status = p.kind; renderStatus(); break;
      case 'running': state.monitoring = !!p.on; break;
      case 'log': pushLog(p); renderLog(); break;
      case 'champ_select': enterSelecting(); renderMonitor(); renderOverlay(); break;
      case 'champ':
        if (p.champ !== state.champ) state.imported = false;  // only invalidate the badge on a real champ change
        state.champ = p.champ; state.champMeta = p.meta; state.selecting = false; renderMonitor(); break;
      case 'matchup':
        state.champ = p.champ || state.champ; state.enemy = p.enemy; state.wr = p.wr;
        state.wrLabel = p.label; state.wrTag = p.tag; state.sample = p.sample; state.selecting = false;
        renderMonitor(); renderOverlay(); break;
      case 'duo_recs':
        state.duo = (p && p.active) ? p : null; renderMonitor(); break;
      case 'rune_page': state.runes = p; renderMonitor(); break;
      case 'build': state.buildSrc = p.src; state.build = p.items || []; renderMonitor(); break;
      case 'import_ok': state.imported = true; renderMonitor(); break;
      case 'game':
        state.inGame = !!p.in_game;
        if (p.in_game) state.selecting = false; else state.hud = null;
        renderOverlay(); renderMonitor(); break;
      case 'hud': state.hud = (p && p.me) ? p : null; renderHud(); break;
      case 'history_sync': state.history.syncing = !!p.active; renderHistory(); break;
      case 'history_updated': state.history.loaded = false; loadHistory(true); break;
      case 'history_error': state.history.error = p.message || 'history sync failed'; renderHistory(); break;
      case 'postgame_ready':
        state.history.loaded = false; loadHistory(true); openReport(p.game_id); break;
      case 'logrec': onLogrec(p); break;
      default: console.log('push?', event, p);
    }
  }

  // ── backend state hydration ────────────────────────────────────────────────
  function applyState(s) {
    if (!s) return;
    state.status = s.status; state.monitoring = !!s.running;
    state.settings = s.settings || state.settings;
    state.builds = s.builds || []; state.sel = 0;
    state.log = (s.log || []).map(r => ({ ts: r.ts, msg: r.msg, cls: r.cls || '' }));
    // Seed the anchor count from hydrated history so reloading doesn't yank the
    // log down to an old matchup line — only genuinely new ones should scroll.
    state._logAnchorCount = state.log.reduce((n, l) => n + (isAnchorLog(l.msg) ? 1 : 0), 0);
    state.champ = s.champ || ''; state.champMeta = s.champMeta || '[ awaiting champ select ]';
    state.imported = !!s.imported; state.selecting = !!s.selecting;
    state.enemy = s.enemy || ''; state.wr = (s.wr == null ? null : s.wr);
    state.wrLabel = s.wrLabel || ''; state.wrTag = s.wrTag || 'info'; state.sample = s.sample || '';
    if (s.runes) state.runes = s.runes;
    state.buildSrc = s.buildSrc || 'idle'; state.build = s.build || []; state.inGame = !!s.inGame;
    state.duo = (s.duo && s.duo.active) ? s.duo : null;
    state.hud = (s.hud && s.hud.me) ? s.hud : null;
    state.history.error = s.historyError || state.history.error;
    applyInterface(state.settings.interface_style);
    applyTheme(s.theme || state.settings.phosphor);
    renderAll();
  }
  function enterSelecting() {
    Object.assign(state, {
      selecting: true,
      champ: '', champMeta: '[ in champ select · selecting… ]', imported: false,
      enemy: '', wr: null, wrLabel: '', wrTag: 'info', sample: '', buildSrc: 'idle', build: [],
      duo: null,
      runes: { keystone: '', primary: '', secondary: '', primaryMinor: '', secondaryMinor: '', summoners: '' }
    });
  }
  function idle() {
    Object.assign(state, {
      champ: '', champMeta: '[ awaiting champ select ]', imported: false, selecting: false,
      enemy: '', wr: null, wrLabel: '', sample: '', buildSrc: 'idle', build: [], log: [], inGame: false,
      duo: null,
      runes: { keystone: '', primary: '', secondary: '', primaryMinor: '', secondaryMinor: '', summoners: '' }
    });
  }
  let _pollTimer = null;
  function connectBackend() {
    idle(); renderAll();
    window.API.call('get_state').then(applyState);
    // PULL model: drain queued Python->JS events on a timer (the backend can't
    // safely call evaluate_js from its monitor threads under edgechromium).
    if (_pollTimer) clearInterval(_pollTimer);
    _pollTimer = setInterval(() => {
      window.API.call('poll_events').then(evts => {
        if (evts && evts.length) evts.forEach(e => handlePush(e.event, e.payload));
      });
    }, 200);
  }

  // ── boot ───────────────────────────────────────────────────────────────────
  function boot() {
    applyInterface(state.settings.interface_style);
    applyTheme(state.settings.phosphor);
    wire();
    setScreen(state.screen);
    renderAll();
    // Under pywebview, pull real state (and clear the placeholder seed). The
    // api may attach slightly after DOMContentLoaded → wait for pywebviewready.
    if (window.pywebview && window.pywebview.api) connectBackend();
    else window.addEventListener('pywebviewready', connectBackend, { once: true });
  }
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', boot);
  else boot();
})();
