/* ===== migrated source: settings.js ===== */
// ══════════════════════════════════════════════════════
//  settings.js — owner-scoped model-routing v2 settings state
//  Brand SVG paths from LobeHub Icons (MIT License)
//  https://github.com/lobehub/lobe-icons
// ══════════════════════════════════════════════════════

/** Cached server config loaded on first openSettings() */
var _serverConfig = null;

/**
 * Read-only USD-pivot rates supplied by GET /api/v1/server-config.
 * The typed modelPricePresentation service owns conversion/formatting; this
 * retained shell only holds the latest server snapshot for its adapters.
 */
var _modelPriceDisplayPolicy = {
  base_currency: 'USD',
  usd_rates: { USD: 1 },
  updated_at: 0,
  source: 'unavailable',
};

/* The Settings editor stages exactly one owner-scoped v2 aggregate. Secret
 * plaintext is held only until the dedicated secret operation succeeds. */
let _stgModelRouting = null;
let _stgModelRoutingRevision = 0;
let _stgModelRoutingLoadError = '';
let _stgPendingCredentialSecrets = {};
let _stgPresets = {};

Object.defineProperties(runtimeScope, {
  _stgModelRouting: {
    configurable: true,
    get: function () { return _stgModelRouting; },
    set: function (value) { _stgModelRouting = value; },
  },
  _stgModelRoutingRevision: {
    configurable: true,
    get: function () { return _stgModelRoutingRevision; },
    set: function (value) { _stgModelRoutingRevision = Number(value || 0); },
  },
});

/* ═══════════════════════════════════════════════════════════════════
   The body of this file (openSettings, saveSettings, _renderProvidersTab,
   _oauth*, _mcp*, ...) lives in the `frontend/src/runtime/settings/` subpackage.
   The bundler concatenates them in load order (see Vite's module graph)
   so symbols are available in window scope by the time index.html
   wires onclick handlers.
   ═══════════════════════════════════════════════════════════════════ */
