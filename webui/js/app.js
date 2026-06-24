/* DAEMON frontend logic. P1: state-driven render + routing + theme + keys,
   seeded with placeholder data so the shell matches the mock. P2 feeds real
   data through window.rs_push() and the API wrappers. */
(function () {
  'use strict';

  const PHOSPHORS = ['amber', 'green', 'ice'];
  const RANKS = ['Iron+', 'Bronze+', 'Silver+', 'Gold+', 'Platinum+', 'Emerald+', 'Diamond+', 'Master+'];
  const REGIONS = ['World', 'NA', 'EUW', 'EUNE', 'KR', 'BR', 'JP', 'OCE', 'LAS', 'LAN', 'TR', 'RU'];
  const PROMPTS = {
    monitor: 'watch --champ-select', builds: 'edit builds.ledger', settings: 'vim daemon.conf'
  };

  // ── state (placeholder seed = the mock's sample data) ─────────────────────
  const state = {
    screen: 'monitor',
    status: 'waiting',                 // booting|connecting|connected|monitoring|waiting
    monitoring: false,
    inGame: false,
    champ: 'JINX', champMeta: '[ locked · bottom lane · marksman ]', imported: true,
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
      { i: 1, name: "Doran's Blade", tag: 'start' },
      { i: 2, name: 'Kraken Slayer', tag: 'core ←', core: true },
      { i: 3, name: "Berserker's Greaves", tag: 'boots' },
      { i: 4, name: 'Infinity Edge', tag: 'core' }
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
    settings: { rank: 'Platinum+', region: 'World', auto_role: true, trigger: 'hover', phosphor: 'amber', autostart: false }
  };

  const $ = (id) => document.getElementById(id);
  const esc = (s) => String(s).replace(/[&<>]/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;' }[c]));

  // ── render ────────────────────────────────────────────────────────────────
  function renderStatus() {
    const el = $('status');
    el.classList.toggle('waiting', state.status === 'waiting');
    $('statusLabel').textContent = 'LEAGUE: ' + state.status.toUpperCase();
  }

  function renderMonitor() {
    $('champName').textContent = state.champ || '—';
    $('champMeta').textContent = state.champMeta || '[ awaiting champ select ]';
    $('champBadge').hidden = !state.imported;

    $('matchupTitle').textContent = state.enemy ? `MATCHUP // vs ${state.enemy}` : 'MATCHUP // idle';
    const down = state.wr != null && state.wr < 50;
    $('wrNum').innerHTML = state.wr == null ? '—' : `${state.wr.toFixed(1)}<small>%</small>`;
    const dir = $('wrDir');
    dir.textContent = state.wr == null ? 'awaiting matchup' : `${down ? '▼' : '▲'} ${state.wrLabel || ''}`.trim();
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
    $('buildList').innerHTML = state.build.length
      ? state.build.map(b =>
          `<div class="brow${b.core ? ' core' : ''}"><span class="bi">${b.i}</span>&nbsp; ${esc(b.name)}` +
          (b.tag ? `<span class="btag">${esc(b.tag)}</span>` : '') + `</div>`).join('')
      : `<div class="brow"><span class="bi">—</span></div>`;

    renderLog();
  }

  function renderLog() {
    const box = $('logBox');
    box.innerHTML = state.log.map(l =>
      `<div><span class="ts">${l.ts}</span>&nbsp; <span class="${l.cls}">${esc(l.msg)}</span></div>`).join('');
    box.scrollTop = box.scrollHeight;
  }

  function renderBuilds() {
    $('buildsCount').textContent = `${state.builds.length} champions · everyone else follows u.gg`;
    const rows = $('ledgerRows');
    if (!state.builds.length) {
      rows.innerHTML = `<div class="ledger-empty">no custom builds yet — press [a] to inscribe one</div>`;
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

  function renderOverlay() {
    $('overlay').hidden = !state.inGame;
    if (!state.inGame) return;
    $('ovMatch').innerHTML = `${esc(state.champ || '—')} <span class="vs">vs</span> ${esc(state.enemy || '—')}`;
    const down = state.wr != null && state.wr < 50;
    $('ovWr').innerHTML = `${state.wr == null ? '—' : state.wr.toFixed(1)}%  <span class="arr">${down ? '▼' : '▲'}</span>`;
    $('ovCtx').textContent = state.wrLabel ? state.wrLabel.toLowerCase() : 'awaiting matchup';
  }

  function setScreen(name) {
    if (!PROMPTS[name]) return;
    state.screen = name;
    document.querySelectorAll('[data-view]').forEach(v => { v.hidden = v.dataset.view !== name; });
    document.querySelectorAll('.tab').forEach(t => t.classList.toggle('active', t.dataset.screen === name));
    $('promptHint').textContent = PROMPTS[name];
  }

  function applyTheme(name) {
    document.documentElement.setAttribute('data-phosphor', name);
    state.settings.phosphor = name;
    renderSettings();
  }

  function renderAll() {
    renderStatus(); renderMonitor(); renderBuilds(); renderSettings(); renderOverlay();
  }

  // ── commands ────────────────────────────────────────────────────────────
  function toggleMonitoring() {
    if (window.API.ready()) {
      // backend confirms via the 'running' push — don't flip optimistically,
      // so a failed start (not connected) doesn't leave a wrong label.
      window.API.call(state.monitoring ? 'stop_monitoring' : 'start_monitoring');
    } else {
      state.monitoring = !state.monitoring;
      $('startWord').textContent = state.monitoring ? 'stop' : 'start';
    }
  }
  function toggleOverlay() { state.inGame = !state.inGame; renderOverlay(); }
  function cmd(action) {
    if (action === 'toggle') toggleMonitoring();
    else if (action === 'overlay') toggleOverlay();
    else if (action === 'tray') window.API.call('hide_to_tray');
    else if (action === 'reimport') window.API.call('reimport');
    else if (action === 'clear') { state.log = []; renderMonitor(); }
    else if (action === 'add' || action === 'edit' || action === 'delete') {
      console.log('builds action (wired in a later phase):', action);
    }
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
  function onKey(e) {
    if (e.ctrlKey || e.altKey || e.metaKey || typing(e)) return;
    const k = e.key.toLowerCase();
    if (k === '1') setScreen('monitor');
    else if (k === '2') setScreen('builds');
    else if (k === '3') setScreen('settings');
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

  function cycle(arr, cur) { return arr[(arr.indexOf(cur) + 1) % arr.length]; }

  function wire() {
    document.querySelectorAll('.tab').forEach(t =>
      t.addEventListener('click', () => setScreen(t.dataset.screen)));
    document.querySelectorAll('[data-cmd]').forEach(el =>
      el.addEventListener('click', () => cmd(el.dataset.cmd)));
    $('status').addEventListener('click', toggleMonitoring);
    document.querySelectorAll('.ledger-row, #ledgerRows').forEach(() => {});
    $('ledgerRows').addEventListener('click', (e) => {
      const row = e.target.closest('.ledger-row'); if (row) { state.sel = +row.dataset.idx; renderBuilds(); }
    });
    // settings (P1: client-side cycling/toggles; P4 wires to backend + menus)
    $('setRank').addEventListener('click', () => { state.settings.rank = cycle(RANKS, state.settings.rank); renderSettings(); });
    $('setRegion').addEventListener('click', () => { state.settings.region = cycle(REGIONS, state.settings.region); renderSettings(); });
    $('setPhosphor').addEventListener('click', () => {
      const next = cycle(PHOSPHORS, state.settings.phosphor);
      applyTheme(next);
      window.API.call('set_theme', next);   // persist immediately
    });
    $('setAutoRole').addEventListener('click', () => { state.settings.auto_role = !state.settings.auto_role; renderSettings(); });
    $('setAutostart').addEventListener('click', () => { state.settings.autostart = !state.settings.autostart; renderSettings(); });
    document.querySelectorAll('[data-trig]').forEach(el =>
      el.addEventListener('click', () => { state.settings.trigger = el.dataset.trig; renderSettings(); }));
    $('saveBtn').addEventListener('click', () => {
      window.API.call('save_settings', JSON.parse(JSON.stringify(state.settings)));
      const m = $('saveMsg'); m.hidden = false; clearTimeout(m._t); m._t = setTimeout(() => { m.hidden = true; }, 2600);
    });
    $('moGo').addEventListener('click', submitMatchup);
    $('moInput').addEventListener('keydown', (e) => { if (e.key === 'Enter') submitMatchup(); });
    window.addEventListener('keydown', onKey);
  }
  function submitMatchup() {
    const v = $('moInput').value.trim(); if (!v) return;
    $('moInput').value = '';
    window.API.call('set_matchup_override', v);
  }

  // ── Python → JS push channel ───────────────────────────────────────────────
  window.rs_push = function (event, payload) {
    try { handlePush(event, payload || {}); }
    catch (e) { console.error('rs_push error', event, e); }
  };
  function pushLog(rec) {
    state.log.push({ ts: rec.ts, msg: rec.msg, cls: rec.cls || '' });
    if (state.log.length > 300) state.log = state.log.slice(-250);
  }
  function handlePush(event, p) {
    switch (event) {
      case 'status': state.status = p.kind; renderStatus(); break;
      case 'running': state.monitoring = !!p.on; $('startWord').textContent = p.on ? 'stop' : 'start'; break;
      case 'log': pushLog(p); renderLog(); break;
      case 'champ': state.champ = p.champ; state.champMeta = p.meta; state.imported = false; renderMonitor(); break;
      case 'matchup':
        state.champ = p.champ || state.champ; state.enemy = p.enemy; state.wr = p.wr;
        state.wrLabel = p.label; state.wrTag = p.tag; state.sample = p.sample;
        renderMonitor(); renderOverlay(); break;
      case 'rune_page': state.runes = p; renderMonitor(); break;
      case 'build': state.buildSrc = p.src; state.build = p.items || []; renderMonitor(); break;
      case 'import_ok': state.imported = true; renderMonitor(); break;
      case 'game': state.inGame = !!p.in_game; renderOverlay(); break;
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
    state.champ = s.champ || ''; state.champMeta = s.champMeta || '[ awaiting champ select ]';
    state.imported = !!s.imported;
    state.enemy = s.enemy || ''; state.wr = (s.wr == null ? null : s.wr);
    state.wrLabel = s.wrLabel || ''; state.wrTag = s.wrTag || 'info'; state.sample = s.sample || '';
    if (s.runes) state.runes = s.runes;
    state.buildSrc = s.buildSrc || 'idle'; state.build = s.build || []; state.inGame = !!s.inGame;
    $('startWord').textContent = state.monitoring ? 'stop' : 'start';
    applyTheme(s.theme || state.settings.phosphor);
    renderAll();
  }
  function idle() {
    Object.assign(state, {
      champ: '', champMeta: '[ awaiting champ select ]', imported: false,
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
