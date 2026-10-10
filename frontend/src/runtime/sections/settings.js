/* ===== migrated source: settings.js ===== */
// ══════════════════════════════════════════════════════
//  settings.js — owner-scoped model-routing v2 settings state
//  Brand SVG paths from LobeHub Icons (MIT License)
//  https://github.com/lobehub/lobe-icons
// ══════════════════════════════════════════════════════

/** Cached server config loaded on first openSettings() */
var _serverConfig = null;

/**
 * Read-only USD-pivot rates supplied by GET /api/v4/server-config.
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
/* JSON of the staged document as last committed by the server (authority
 * load or successful replace). Drives the dirty check that skips no-op PUTs. */
let _stgModelRoutingSavedJson = '';
/* Serializes immediate routing persists (model adds). Every queued pass
 * re-reads the latest staged doc + revision, so passes cannot CAS-conflict
 * and edits staged mid-flight are picked up by the next pass. */
let _stgRoutingPersistChain = null;
let _stgModelRoutingLoadError = '';
let _stgModelRoutingLoadPromise = null;
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
Object.defineProperties(runtimeScope, {
  _stgModelRoutingSavedJson: {
    configurable: true,
    get: function () { return _stgModelRoutingSavedJson; },
    set: function (value) { _stgModelRoutingSavedJson = String(value || ''); },
  },
  _stgRoutingPersistChain: {
    configurable: true,
    get: function () { return _stgRoutingPersistChain; },
    set: function (value) { _stgRoutingPersistChain = value; },
  },
});
Object.defineProperties(runtimeScope, {
  _stgModelRoutingLoadError: {
    configurable: true,
    get: function () { return _stgModelRoutingLoadError; },
    set: function (value) { _stgModelRoutingLoadError = String(value || ''); },
  },
  _stgModelRoutingLoadPromise: {
    configurable: true,
    get: function () { return _stgModelRoutingLoadPromise; },
    set: function (value) { _stgModelRoutingLoadPromise = value; },
  },
  _stgPendingCredentialSecrets: {
    configurable: true,
    get: function () { return _stgPendingCredentialSecrets; },
    set: function (value) { _stgPendingCredentialSecrets = value || {}; },
  },
  _stgPresets: {
    configurable: true,
    get: function () { return _stgPresets; },
    set: function (value) { _stgPresets = value || {}; },
  },
  // Declared with var in settings/other_tabs.js; the concatenated module
  // hoists the binding, so this accessor stays live across both owners.
  _searchLastPreset: {
    configurable: true,
    get: function () { return _searchLastPreset; },
    set: function (value) { _searchLastPreset = value; },
  },
});

/* ═══════════════════════════════════════════════════════════════════
   The body of this file (openSettings, saveSettings, _renderProvidersTab,
   _oauth*, _mcp*, ...) lives in the `frontend/src/runtime/settings/` subpackage.
   The bundler concatenates them in load order (see Vite's module graph)
   so symbols are available in window scope by the time index.html
   wires onclick handlers.
   ═══════════════════════════════════════════════════════════════════ */
