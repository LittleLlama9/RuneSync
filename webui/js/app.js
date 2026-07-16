/* DAEMON frontend logic — state-driven render + routing + theme + keys.
   Seeded with placeholder data (preview/standalone); under pywebview, get_state
   hydrates and a 200ms poll loop drains live events via poll_events() -> handlePush. */
(function () {
  'use strict';

  const PHOSPHORS = ['amber', 'green', 'ice'];
  const RANKS = ['Iron+', 'Bronze+', 'Silver+', 'Gold+', 'Platinum+', 'Emerald+', 'Diamond+', 'Master+'];
  const REGIONS = ['World', 'NA', 'EUW', 'EUNE', 'KR', 'BR', 'JP', 'OCE', 'LAS', 'LAN', 'TR', 'RU'];
  const PROMPTS = {
    monitor: 'watch --champ-select', builds: 'edit builds.ledger',
    settings: 'vim daemon.conf', history: 'query match.history',
    report: 'cat postgame.report', editor: 'vim override', builder: 'edit build',
    debug: 'tail -f runesync.log'
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
    settings: { rank: 'Platinum+', region: 'World', auto_role: true, trigger: 'hover', phosphor: 'amber', autostart: false }
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
    const selecting = state.selecting && !state.champ;
    $('champName').textContent = state.champ || (selecting ? 'SELECTING…' : '—');
    $('champMeta').textContent = state.champ
      ? (state.champMeta || '')
      : (selecting ? '[ in champ select · pick a champion ]'
                   : (state.champMeta || '[ awaiting champ select ]'));
    $('champBadge').hidden = !state.imported;

    $('matchupTitle').textContent = state.enemy
      ? `MATCHUP // vs ${state.enemy}`
      : (state.selecting ? 'MATCHUP // awaiting pick' : 'MATCHUP // idle');
    const down = state.wr != null && state.wr < 50;
    $('wrNum').innerHTML = state.wr == null ? '—' : `${state.wr.toFixed(1)}<small>%</small>`;
    const dir = $('wrDir');
    dir.textContent = state.wr == null
      ? (state.enemy ? (state.wrLabel || 'win rate unavailable')
                     : (state.selecting ? 'awaiting pick' : 'awaiting matchup'))
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

    $('buildTitle').textContent = `BUILD // ${state.buildSrc}`;
    $('buildList').innerHTML = renderBuildList(state.build);

    renderLog();
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
    $('buildsCount').textContent = `${state.builds.length} champions · everyone else follows u.gg`;
    const rows = $('ledgerRows');
    if (!state.builds.length) {
      rows.innerHTML = `<div class="ledger-empty">no custom builds yet — press [a] to add one</div>`;
      return;
    }
    rows.innerHTML = state.builds.map((b, i) =>
      `<div class="ledger-row${i === state.sel ? ' sel' : ''}" data-idx="${i}">` +
      `<span class="num">${String(i + 1).padStart(2, '0')}</span>` +
      `<span>${esc(b.champ)}</span><span>${esc(b.role)}</span>` +
      `<span class="sm">${esc(b.path)}</span><span class="sm">${esc(b.summoners)}</span></div>`).join('');
  }

  function renderSettings() {
    const s = state.settings;
    $('setRank').textContent = s.rank + ' ▾';
    $('setRegion').textContent = s.region + ' ▾';
    $('setAutoRole').textContent = s.auto_role ? '[x]' : '[ ]';
    $('setPhosphor').textContent = s.phosphor + ' ▾';
    $('setAutostart').textContent = s.autostart ? '[x]' : '[ ]';
    document.querySelectorAll('[data-trig]').forEach(el => {
      el.textContent = (el.dataset.trig === s.trigger ? '(•)' : '( )') +
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
    $('historyLoaded').textContent = `${h.rows.length} / ${total}`;
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
        const cardLabel = `${result}, ${row.local_champion_name}, ${row.local_role}, DAEMON score ${score.toFixed(1)}, match rank ${rank} of 10`;
        return `<article class="history-card ${win ? 'is-win' : 'is-loss'}" data-game="${row.game_id}" ` +
          `role="button" tabindex="0" aria-label="${esc(cardLabel)}">` +
          `<div class="history-result"><strong>${win ? 'W' : 'L'}</strong><span>${win ? 'VICTORY' : 'DEFEAT'}</span></div>` +
          `<div class="history-match">` +
            `<div class="history-match-title"><strong>${esc(row.local_champion_name)}</strong>` +
              `<span>${esc(row.local_role)}</span></div>` +
            `<div class="history-kda"><b>${row.kills}/${row.deaths}/${row.assists}</b><span>KDA</span></div>` +
            `<div class="history-match-meta">${queueLabel(row.queue_id)} · ${minutes}:${seconds} · ${historyWhen(row.game_creation_date)}</div>` +
          `</div>` +
          `<div class="history-score"><span>DAEMON</span><strong>${score.toFixed(1)}</strong><em>${scoreBand(score)}</em></div>` +
          `<div class="history-rank ${rankClass(rank)}"><span>MATCH RANK</span><strong>#${rank}</strong><em>${rankLabel(rank)}</em></div>` +
        `</article>`;
      }).join('');
    }
    h.hasMore = h.rows.length < total;
    const more = $('historyMore');
    const moreDisabled = !h.hasMore || h.loading;
    more.classList.toggle('disabled', moreDisabled);
    more.setAttribute('aria-disabled', String(moreDisabled));
    more.tabIndex = moreDisabled ? -1 : 0;
    more.textContent = h.loading
      ? '[loading archive...]'
      : h.hasMore
        ? `[load ${Math.min(12, total - h.rows.length)} more]`
        : `[archive complete // ${h.rows.length} matches]`;
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
    $('reportHero').className = `report-hero ${local.win ? 'is-win' : 'is-loss'} ${rankClass(local.match_rank)}`;
    $('reportTitle').textContent = `${local.champion_name} · ${local.role}`;
    const minutes = Math.floor(match.duration / 60);
    const seconds = String(match.duration % 60).padStart(2, '0');
    $('reportMeta').textContent = `${local.kills}/${local.deaths}/${local.assists} · ${local.cs} CS · ${minutes}:${seconds} · ${match.patch}`;
    $('reportScore').textContent = Number(local.total_score).toFixed(1);
    $('reportScoreBand').textContent = `${scoreBand(Number(local.total_score))} PERFORMANCE`;
    $('reportRank').textContent = `#${local.match_rank}`;
    $('reportRankLabel').textContent = `${rankLabel(local.match_rank)} // OF 10`;
    $('reportModel').textContent = local.model_version;
    const labels = {
      combat: 'combat', economy: 'economy', objectives: 'objectives',
      vision: 'vision', teamplay: 'survival/teamplay'
    };
    $('reportComponents').innerHTML = Object.entries(local.components || {}).map(([key, value]) =>
      `<div class="component-row"><span>${labels[key] || key}</span>` +
      `<div class="component-track"><i style="width:${Math.max(0, Math.min(100, value))}%"></i></div>` +
      `<b>${Number(value).toFixed(1)}</b></div>`
    ).join('');
    $('reportObservations').innerHTML = (local.observations || [])
      .map(text => `<div>&gt; ${esc(text)}</div>`).join('');
    const localTeam = local.team_id;
    let previousTeam = null;
    $('reportPlayers').innerHTML = report.participants.map(player => {
      const teamHeader = player.team_id !== previousTeam
        ? `<div class="report-team">${player.team_id === localTeam ? 'YOUR TEAM' : 'ENEMY TEAM'}</div>`
        : '';
      previousTeam = player.team_id;
      const rank = Number(player.match_rank);
      return teamHeader +
        `<div class="report-player ${rankClass(rank)}${player.participant_id === match.local_participant_id ? ' local' : ''}">` +
        `<span class="player-rank">#${rank}<i>${rankLabel(rank)}</i></span><span>${esc(player.summoner_name)}</span>` +
        `<span>${esc(player.champion_name)}</span><span>${esc(player.role)}</span>` +
        `<span>${player.kills}/${player.deaths}/${player.assists}</span>` +
        `<span class="player-score">${Number(player.total_score).toFixed(1)}</span></div>`;
    }).join('');
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
    $('promptHint').textContent = PROMPTS[name];
    if (name === 'history') {
      setHistorySection(state.history.section);
      if (!state.history.loaded) loadHistory(true);
    }
    const body = $('viewBody');
    if (body && body.focus) body.focus({ preventScroll: true });
  }

  function applyTheme(name) {
    document.documentElement.setAttribute('data-phosphor', name);
    state.settings.phosphor = name;
    renderSettings();
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
    document.querySelectorAll('[role="button"]').forEach(el =>
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
    $('setPhosphor').addEventListener('click', () =>
      openMenu($('setPhosphor'), PHOSPHORS, state.settings.phosphor, v => {
        applyTheme(v); window.API.call('set_theme', v);   // theme persists immediately
      }));
    $('setAutoRole').addEventListener('click', () => { state.settings.auto_role = !state.settings.auto_role; renderSettings(); });
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
  let _menuEl = null;
  function closeMenu() {
    if (_menuEl) { _menuEl.remove(); _menuEl = null; document.removeEventListener('mousedown', _menuOutside, true); }
  }
  function _menuOutside(e) { if (_menuEl && !_menuEl.contains(e.target)) closeMenu(); }
  function openMenu(anchor, options, current, onPick) {
    closeMenu();
    const m = document.createElement('div'); m.className = 'menu-pop';
    options.forEach(opt => {
      const label = Array.isArray(opt) ? opt[0] : opt;
      const value = Array.isArray(opt) ? opt[1] : opt;
      const it = document.createElement('div');
      it.className = 'mi' + (value === current ? ' on' : ''); it.textContent = label;
      it.addEventListener('click', () => { onPick(value); closeMenu(); });
      m.appendChild(it);
    });
    document.body.appendChild(m);
    const r = anchor.getBoundingClientRect();
    m.style.left = r.left + 'px'; m.style.top = (r.bottom + 2) + 'px';
    setTimeout(() => document.addEventListener('mousedown', _menuOutside, true), 0);
    _menuEl = m;
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
      case 'rune_page': state.runes = p; renderMonitor(); break;
      case 'build': state.buildSrc = p.src; state.build = p.items || []; renderMonitor(); break;
      case 'import_ok': state.imported = true; renderMonitor(); break;
      case 'game': state.inGame = !!p.in_game; if (p.in_game) state.selecting = false; renderOverlay(); break;
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
    state.history.error = s.historyError || state.history.error;
    applyTheme(s.theme || state.settings.phosphor);
    renderAll();
  }
  function enterSelecting() {
    Object.assign(state, {
      selecting: true,
      champ: '', champMeta: '[ in champ select · selecting… ]', imported: false,
      enemy: '', wr: null, wrLabel: '', wrTag: 'info', sample: '', buildSrc: 'idle', build: [],
      runes: { keystone: '', primary: '', secondary: '', primaryMinor: '', secondaryMinor: '', summoners: '' }
    });
  }
  function idle() {
    Object.assign(state, {
      champ: '', champMeta: '[ awaiting champ select ]', imported: false, selecting: false,
      enemy: '', wr: null, wrLabel: '', sample: '', buildSrc: 'idle', build: [], log: [], inGame: false,
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
