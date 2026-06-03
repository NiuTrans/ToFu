// ══════════════════════════════════════════════════════
//  settings.js — Multi-provider settings with nested models
//  Brand SVG paths from LobeHub Icons (MIT License)
//  https://github.com/lobehub/lobe-icons
// ══════════════════════════════════════════════════════

/** Cached server config loaded on first openSettings() */
var _serverConfig = null;

/** Cached today's per-key success/failure stats: { day, providers: {pid: {key_name: {...}}} } */
var _keyStatsCache = {
  day: '', providers: {},
  min_attempts: 5, min_success_rate: 0.5, max_consecutive_429: 100,
};
var _keyStatsLoading = false;


/* ═══════════════════════════════════════════════════════════════════
   The body of this file (openSettings, saveSettings, _renderProvidersTab,
   _oauth*, _mcp*, ...) lives in the `static/js/settings/` subpackage.
   The bundler concatenates them in load order (see lib/js_bundler.py)
   so symbols are available in window scope by the time index.html
   wires onclick handlers.
   ═══════════════════════════════════════════════════════════════════ */
