/* Thin bridge to the Python backend. In P1 there is no js_api yet, so every
   call no-ops safely; P2 wires these to window.pywebview.api.* (Promises). */
(function () {
  function ready() { return !!(window.pywebview && window.pywebview.api); }
  function call(name, ...args) {
    if (!ready()) return Promise.resolve(null);
    const fn = window.pywebview.api[name];
    if (typeof fn !== 'function') return Promise.resolve(null);
    try { return Promise.resolve(fn(...args)); }
    catch (e) { console.error('api call failed:', name, e); return Promise.resolve(null); }
  }
  window.API = { ready, call };
})();
