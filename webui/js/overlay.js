/* RuneSync champ-select overlay frontend.

   Renders the champ-select panels the backend already computes — matchup win
   rate, counter picks, and the draft/composition read — into the compact
   always-on-top panel docked to the League client. Same PULL model as the main
   window: hydrate via get_overlay_state, then drain poll_overlay_events on a
   timer. Read-only; it never calls any action endpoint. */
(function () {
  'use strict';

  const $ = id => document.getElementById(id);
  const esc = s => String(s == null ? '' : s).replace(/[&<>"']/g,
    c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));

  const state = {
    running: false,
    selecting: false,
    champ: '',
    enemy: '',
    wr: null,
    wrLabel: '',
    wrTag: 'info',
    sample: '',
    counters: null,   // {enemy, counters:[{champion,win_rate,games}], active}
    draft: null,      // {observations:[{level,text}]}
    inGame: false
  };

  function fmtGames(n) {
    n = Number(n) || 0;
    return n >= 1000 ? (n / 1000).toFixed(n >= 10000 ? 0 : 1) + 'k' : String(n);
  }

  function applyTheme(theme, iface) {
    const root = document.documentElement;
    if (theme) root.setAttribute('data-phosphor', theme);
    if (iface) root.setAttribute('data-interface', iface);
  }

  // ── render ────────────────────────────────────────────────────────────────
  function renderWr() {
    const sec = $('ovWrSec');
    const hasWr = state.wr != null && state.enemy;
    if (!hasWr) { sec.hidden = true; return; }
    sec.hidden = false;
    const wr = Number(state.wr);
    const cls = wr >= 52 ? 'win' : wr <= 48 ? 'loss' : 'even';
    const labelCls = state.wrTag === 'success' ? 'win' : state.wrTag === 'error' ? 'loss' : '';
    const you = state.champ ? `<b>${esc(state.champ)}</b> vs ` : '';
    $('ovWr').innerHTML =
      `<div class="ov-wr-main"><span class="ov-wr-pct ${cls}">${wr.toFixed(1)}%</span>` +
      (state.wrLabel ? `<span class="ov-wr-label ${labelCls}">${esc(state.wrLabel)}</span>` : '') +
      `</div>` +
      `<div class="ov-wr-vs">${you}<b>${esc(state.enemy)}</b></div>`;
    $('ovWrSample').textContent = state.sample || '';
  }

  function renderCounters() {
    const sec = $('ovCountersSec');
    const c = state.counters;
    const rows = (c && c.active && c.counters) ? c.counters : [];
    if (!rows.length) { sec.hidden = true; return; }
    sec.hidden = false;
    $('ovCountersHead').textContent = c.enemy ? `COUNTERS vs ${String(c.enemy).toUpperCase()}` : 'COUNTERS';
    $('ovCounters').innerHTML = rows.slice(0, 5).map(r => {
      const wr = r.win_rate != null ? `${Number(r.win_rate).toFixed(1)}%` : '—';
      const games = r.games != null ? `<span class="ov-c-games">${fmtGames(r.games)}g</span>` : '';
      return `<div class="ov-counter"><span class="ov-c-name">${esc(r.champion)}</span>` +
        `<span><span class="ov-c-wr">${wr}</span>${games}</span></div>`;
    }).join('');
    $('ovCountersSample').textContent = c.sample || '';
  }

  function renderDraft() {
    const sec = $('ovDraftSec');
    const d = state.draft;
    const obs = (d && d.observations) ? d.observations : [];
    // Draft analysis is champ-select only; hide once in game.
    if (!obs.length || state.inGame) { sec.hidden = true; return; }
    sec.hidden = false;
    $('ovDraft').innerHTML = obs.map(o => {
      const lvl = o.level === 'warn' ? 'warn' : o.level === 'good' ? 'good' : 'info';
      const mark = lvl === 'warn' ? '!' : lvl === 'good' ? '✓' : '›';
      return `<div class="ov-draft-row ${lvl}"><span class="ov-d-mark">${mark}</span>` +
        `<span class="ov-d-text">${esc(o.text)}</span></div>`;
    }).join('');
  }

  function renderEmpty() {
    const anyVisible = !$('ovWrSec').hidden || !$('ovCountersSec').hidden || !$('ovDraftSec').hidden;
    $('ovEmpty').hidden = anyVisible;
    $('ovEmpty').textContent = state.running ? 'Waiting for champ select…' : 'Start monitoring in RuneSync';
  }

  function renderAll() {
    renderWr();
    renderCounters();
    renderDraft();
    renderEmpty();
  }

  // ── events ──────────────────────────────────────────────────────────────
  function handlePush(event, p) {
    p = p || {};
    switch (event) {
      case 'running': state.running = !!p.on; break;
      case 'game': state.inGame = !!p.in_game; break;
      case 'champ_select':
        if (p.active) { state.selecting = true; }
        else {
          // Champ select ended — clear the transient panels.
          state.selecting = false; state.enemy = ''; state.wr = null;
          state.wrLabel = ''; state.counters = null; state.draft = null; state.champ = '';
        }
        break;
      case 'champ': state.champ = p.champ || state.champ; break;
      case 'matchup':
        state.champ = p.champ || state.champ; state.enemy = p.enemy || '';
        state.wr = p.wr; state.wrLabel = p.label || ''; state.wrTag = p.tag || 'info';
        state.sample = p.sample || state.sample;
        break;
      case 'counters':
        state.counters = (p && p.active) ? p : null;
        break;
      case 'draft':
        state.draft = (p && p.observations && p.observations.length) ? p : null;
        break;
      default: return;
    }
    renderAll();
  }

  function applyState(s) {
    if (!s) return;
    applyTheme(s.theme, s.interface_style);
    state.running = !!s.running;
    state.selecting = !!s.selecting;
    state.champ = s.champ || '';
    state.enemy = s.enemy || '';
    state.wr = s.wr;
    state.wrLabel = s.wrLabel || '';
    state.wrTag = s.wrTag || 'info';
    state.sample = s.sample || '';
    state.counters = s.counters || null;
    state.draft = (s.draft && s.draft.observations) ? s.draft : null;
    renderAll();
  }

  // ── boot ────────────────────────────────────────────────────────────────
  let _timer = null;
  function connect() {
    window.API.call('get_overlay_state').then(applyState);
    if (_timer) clearInterval(_timer);
    _timer = setInterval(() => {
      window.API.call('poll_overlay_events').then(evts => {
        if (evts && evts.length) evts.forEach(e => handlePush(e.event, e.payload));
      });
    }, 250);
  }

  function boot() {
    renderAll();
    if (window.pywebview && window.pywebview.api) connect();
    else window.addEventListener('pywebviewready', connect, { once: true });
  }
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', boot);
  else boot();
})();
