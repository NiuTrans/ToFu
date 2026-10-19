// @ts-check
/* Generated lazy retained runtime: settings-presenters. Do not edit directly. */
import { featureRegistry as runtimeScope } from '../feature-registry';
import { _i18nLang, _syncLangPicker, t } from '../i18n/index';
import { escapeHtml, raw, safeHtml } from '../html-safety';
import { renderCodexSharedLogin } from '../features/settings/codex-shared-login';

const Api = runtimeScope.Api;
if (!Api || typeof Api !== 'object') throw new Error('settings-presenters runtime dependency is unavailable: Api');
const apiUrl = runtimeScope.apiUrl;
if (typeof apiUrl !== 'function') throw new Error('settings-presenters runtime dependency is unavailable: apiUrl');
const ConvCache = runtimeScope.ConvCache;
if (!ConvCache || typeof ConvCache !== 'object') throw new Error('settings-presenters runtime dependency is unavailable: ConvCache');
const config = runtimeScope.config;
if (!config || typeof config !== 'object') throw new Error('settings-presenters runtime dependency is unavailable: config');
const Icon = runtimeScope.Icon;
if (typeof Icon !== 'function') throw new Error('settings-presenters runtime dependency is unavailable: Icon');
const IconDot = runtimeScope.IconDot;
if (typeof IconDot !== 'function') throw new Error('settings-presenters runtime dependency is unavailable: IconDot');
const _applyFlowUI = runtimeScope._applyFlowUI;
if (typeof _applyFlowUI !== 'function') throw new Error('settings-presenters runtime dependency is unavailable: _applyFlowUI');
const _applyModelUI = runtimeScope._applyModelUI;
if (typeof _applyModelUI !== 'function') throw new Error('settings-presenters runtime dependency is unavailable: _applyModelUI');
const getManualMatrixProbe = runtimeScope.getManualMatrixProbe;
if (typeof getManualMatrixProbe !== 'function') throw new Error('settings-presenters runtime dependency is unavailable: getManualMatrixProbe');
const _persistSttProvider = runtimeScope._persistSttProvider;
if (typeof _persistSttProvider !== 'function') throw new Error('settings-presenters runtime dependency is unavailable: _persistSttProvider');
const _brandSvg = runtimeScope._brandSvg;
if (typeof _brandSvg !== 'function') throw new Error('settings-presenters runtime dependency is unavailable: _brandSvg');
const _compareModelIds = runtimeScope._compareModelIds;
if (typeof _compareModelIds !== 'function') throw new Error('settings-presenters runtime dependency is unavailable: _compareModelIds');
const _configForPersist = runtimeScope._configForPersist;
if (typeof _configForPersist !== 'function') throw new Error('settings-presenters runtime dependency is unavailable: _configForPersist');
const _detectBrand = runtimeScope._detectBrand;
if (typeof _detectBrand !== 'function') throw new Error('settings-presenters runtime dependency is unavailable: _detectBrand');
const _modelBrand = runtimeScope._modelBrand;
if (typeof _modelBrand !== 'function') throw new Error('settings-presenters runtime dependency is unavailable: _modelBrand');
const _getCurrentTheme = runtimeScope._getCurrentTheme;
if (typeof _getCurrentTheme !== 'function') throw new Error('settings-presenters runtime dependency is unavailable: _getCurrentTheme');
const _loadServerConfigAndPopulate = runtimeScope._loadServerConfigAndPopulate;
if (typeof _loadServerConfigAndPopulate !== 'function') throw new Error('settings-presenters runtime dependency is unavailable: _loadServerConfigAndPopulate');
const _modelRoutingDropdownModels = runtimeScope._modelRoutingDropdownModels;
if (typeof _modelRoutingDropdownModels !== 'function') throw new Error('settings-presenters runtime dependency is unavailable: _modelRoutingDropdownModels');
const _modelShortName = runtimeScope._modelShortName;
if (typeof _modelShortName !== 'function') throw new Error('settings-presenters runtime dependency is unavailable: _modelShortName');
const _populateDevicesTab = runtimeScope._populateDevicesTab;
if (typeof _populateDevicesTab !== 'function') throw new Error('settings-presenters runtime dependency is unavailable: _populateDevicesTab');
const _populateModelDropdown = runtimeScope._populateModelDropdown;
if (typeof _populateModelDropdown !== 'function') throw new Error('settings-presenters runtime dependency is unavailable: _populateModelDropdown');
const _populatePreferencesTab = runtimeScope._populatePreferencesTab;
if (typeof _populatePreferencesTab !== 'function') throw new Error('settings-presenters runtime dependency is unavailable: _populatePreferencesTab');
const _populateSkillsTab = runtimeScope._populateSkillsTab;
if (typeof _populateSkillsTab !== 'function') throw new Error('settings-presenters runtime dependency is unavailable: _populateSkillsTab');
const _populateSpeechTab = runtimeScope._populateSpeechTab;
if (typeof _populateSpeechTab !== 'function') throw new Error('settings-presenters runtime dependency is unavailable: _populateSpeechTab');
const _refreshSttStatus = runtimeScope._refreshSttStatus;
if (typeof _refreshSttStatus !== 'function') throw new Error('settings-presenters runtime dependency is unavailable: _refreshSttStatus');
const _renderAuthSources = runtimeScope._renderAuthSources;
if (typeof _renderAuthSources !== 'function') throw new Error('settings-presenters runtime dependency is unavailable: _renderAuthSources');
const _renderModelCatalogTab = runtimeScope._renderModelCatalogTab;
if (typeof _renderModelCatalogTab !== 'function') throw new Error('settings-presenters runtime dependency is unavailable: _renderModelCatalogTab');
const _renderModelRoutingTabs = runtimeScope._renderModelRoutingTabs;
if (typeof _renderModelRoutingTabs !== 'function') throw new Error('settings-presenters runtime dependency is unavailable: _renderModelRoutingTabs');
const _renderPresetsTab = runtimeScope._renderPresetsTab;
if (typeof _renderPresetsTab !== 'function') throw new Error('settings-presenters runtime dependency is unavailable: _renderPresetsTab');
const _collectModelDefaults = runtimeScope._collectModelDefaults;
if (typeof _collectModelDefaults !== 'function') throw new Error('settings-presenters runtime dependency is unavailable: _collectModelDefaults');
const closeSettings = runtimeScope.closeSettings;
if (typeof closeSettings !== 'function') throw new Error('settings-presenters runtime dependency is unavailable: closeSettings');
const _setVal = runtimeScope._setVal;
if (typeof _setVal !== 'function') throw new Error('settings-presenters runtime dependency is unavailable: _setVal');
const _loadModelRoutingAuthority = runtimeScope._loadModelRoutingAuthority;
if (typeof _loadModelRoutingAuthority !== 'function') throw new Error('settings-presenters runtime dependency is unavailable: _loadModelRoutingAuthority');
const _persistModelRoutingNow = runtimeScope._persistModelRoutingNow;
if (typeof _persistModelRoutingNow !== 'function') throw new Error('settings-presenters runtime dependency is unavailable: _persistModelRoutingNow');
const _refreshSubscriptionModelCatalog = runtimeScope._refreshSubscriptionModelCatalog;
if (typeof _refreshSubscriptionModelCatalog !== 'function') throw new Error('settings-presenters runtime dependency is unavailable: _refreshSubscriptionModelCatalog');
const _refreshSystemPromptSummary = runtimeScope._refreshSystemPromptSummary;
if (typeof _refreshSystemPromptSummary !== 'function') throw new Error('settings-presenters runtime dependency is unavailable: _refreshSystemPromptSummary');
const _renderCredentialsVault = runtimeScope._renderCredentialsVault;
if (typeof _renderCredentialsVault !== 'function') throw new Error('settings-presenters runtime dependency is unavailable: _renderCredentialsVault');
const _renderPrivateHosts = runtimeScope._renderPrivateHosts;
if (typeof _renderPrivateHosts !== 'function') throw new Error('settings-presenters runtime dependency is unavailable: _renderPrivateHosts');
const _sortModelEntriesByDisplayName = runtimeScope._sortModelEntriesByDisplayName;
if (typeof _sortModelEntriesByDisplayName !== 'function') throw new Error('settings-presenters runtime dependency is unavailable: _sortModelEntriesByDisplayName');
const _sortModelsByDisplayName = runtimeScope._sortModelsByDisplayName;
if (typeof _sortModelsByDisplayName !== 'function') throw new Error('settings-presenters runtime dependency is unavailable: _sortModelsByDisplayName');
const _sortedBrandKeys = runtimeScope._sortedBrandKeys;
if (typeof _sortedBrandKeys !== 'function') throw new Error('settings-presenters runtime dependency is unavailable: _sortedBrandKeys');
const _warnModelCapsMissing = runtimeScope._warnModelCapsMissing;
if (typeof _warnModelCapsMissing !== 'function') throw new Error('settings-presenters runtime dependency is unavailable: _warnModelCapsMissing');
const applySectionRequirements = runtimeScope.applySectionRequirements;
if (typeof applySectionRequirements !== 'function') throw new Error('settings-presenters runtime dependency is unavailable: applySectionRequirements');
const brandLogoImgAttrs = runtimeScope.brandLogoImgAttrs;
if (typeof brandLogoImgAttrs !== 'function') throw new Error('settings-presenters runtime dependency is unavailable: brandLogoImgAttrs');
const captureActiveConversationSettings = runtimeScope.captureActiveConversationSettings;
if (typeof captureActiveConversationSettings !== 'function') throw new Error('settings-presenters runtime dependency is unavailable: captureActiveConversationSettings');
const debugLog = runtimeScope.debugLog;
if (typeof debugLog !== 'function') throw new Error('settings-presenters runtime dependency is unavailable: debugLog');
const errorEnvelopeMessage = runtimeScope.errorEnvelopeMessage;
if (typeof errorEnvelopeMessage !== 'function') throw new Error('settings-presenters runtime dependency is unavailable: errorEnvelopeMessage');
const newChat = runtimeScope.newChat;
if (typeof newChat !== 'function') throw new Error('settings-presenters runtime dependency is unavailable: newChat');
const updateSendButton = runtimeScope.updateSendButton;
if (typeof updateSendButton !== 'function') throw new Error('settings-presenters runtime dependency is unavailable: updateSendButton');
const getActiveConv = runtimeScope.getActiveConv;
if (typeof getActiveConv !== 'function') throw new Error('settings-presenters runtime dependency is unavailable: getActiveConv');
const refreshInputSendHint = runtimeScope.refreshInputSendHint;
if (typeof refreshInputSendHint !== 'function') throw new Error('settings-presenters runtime dependency is unavailable: refreshInputSendHint');
const renderConversationList = runtimeScope.renderConversationList;
if (typeof renderConversationList !== 'function') throw new Error('settings-presenters runtime dependency is unavailable: renderConversationList');
const showAlert = runtimeScope.showAlert;
if (typeof showAlert !== 'function') throw new Error('settings-presenters runtime dependency is unavailable: showAlert');
const showConfirm = runtimeScope.showConfirm;
if (typeof showConfirm !== 'function') throw new Error('settings-presenters runtime dependency is unavailable: showConfirm');
const showPrompt = runtimeScope.showPrompt;
if (typeof showPrompt !== 'function') throw new Error('settings-presenters runtime dependency is unavailable: showPrompt');
const showToast = runtimeScope.showToast;
if (typeof showToast !== 'function') throw new Error('settings-presenters runtime dependency is unavailable: showToast');
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
/* ===== migrated source: widgets/dropdown.js ===== */
/*
 * Unified dropdown — one styled popup for BOTH native selects and custom
 * popovers.
 *
 * WHY THIS EXISTS
 * ───────────────
 * A native ``<select>`` opens an OS-rendered popup that ignores page CSS
 * entirely, so it always looked like an alien widget inside the tofu shell —
 * the exact complaint that split the app's dropdowns into two visual
 * languages (native OS menu in settings vs. the styled ``.preset-dropdown``
 * in the composer). The only way to unify them is to render the select's
 * popup as DOM too, in the same warm tofu popover language.
 *
 * DESIGN — progressive enhancement, the select stays the source of truth
 * ─────────────────────────────────────────────────────────────────────
 * A select carries ``data-tofu-action-change`` that existing handlers depend
 * on; we must not break that contract. So the enhancement keeps the real
 * ``<select>`` as the value + change authority:
 *   1. Wrap the select in a ``.tofu-dd`` span; hide the native control
 *      visually (it stays focusable/screen-reader visible).
 *   2. Render a styled trigger button + a body-anchored DOM listbox that
 *      mirror the select's ``<option>``s.
 *   3. Choosing an option sets ``select.value`` and dispatches a bubbling
 *      ``change`` — the SAME event a user picking from the native popup would
 *      fire — so every existing ``data-tofu-action-change`` handler runs
 *      unchanged. Per-option ``data-*`` (e.g. MCP ``data-autofill``) rides
 *      along because the option element itself is selected.
 *
 * OPT-IN: only selects with ``data-tofu-dd`` (or ``.tofu-dd-native``) are
 * enhanced; everything else keeps the native popup. Enhancement is idempotent
 * and re-entrant, so it is safe to call after every settings re-render.
 *
 * Behavior mirrors the composer's provider-menu pattern: one open menu at a
 * time, body-anchored so no scrolling ancestor clips it, closes on outside
 * click / scroll / Escape, and flips below the trigger when there is no room
 * above.
 */

var _TOFU_DD_OPEN = null; // the wrapper whose menu is currently open, or null

function _tofuDdSelectOf(wrap) {
  return wrap ? wrap.querySelector('select') : null;
}

function _tofuDdClose() {
  var menu = document.getElementById('tofuDdMenu');
  if (menu) {
    if (typeof menu._tofuDdTeardown === 'function') menu._tofuDdTeardown();
    menu.remove();
  }
  if (_TOFU_DD_OPEN) {
    _TOFU_DD_OPEN.classList.remove('open');
    var trigger = _TOFU_DD_OPEN.querySelector('.tofu-dd-trigger');
    if (trigger) trigger.setAttribute('aria-expanded', 'false');
  }
  _TOFU_DD_OPEN = null;
}

/* Read the select's options into menu entries, preserving order, disabled
 * state, the selected marker, and any per-option label override. */
function _tofuDdOptions(select) {
  var out = [];
  Array.from(select.options).forEach(function(opt, index) {
    out.push({
      index: index,
      value: opt.value,
      label: opt.textContent || opt.value,
      disabled: opt.disabled,
      selected: opt.selected,
    });
  });
  return out;
}

function _tofuDdPosition(wrap, menu) {
  var trigger = wrap.querySelector('.tofu-dd-trigger');
  if (!trigger) return;
  var rect = trigger.getBoundingClientRect();
  var menuWidth = menu.offsetWidth;
  var menuHeight = menu.offsetHeight;
  /* Prefer below the trigger (a select opens downward); flip above when the
   * viewport bottom would clip it. Settings panels scroll, so clamp left. */
  var top = rect.bottom + 5;
  if (top + menuHeight > window.innerHeight - 8 && rect.top - menuHeight - 5 > 8) {
    top = rect.top - menuHeight - 5;
  }
  var left = Math.max(8, Math.min(rect.left, window.innerWidth - menuWidth - 8));
  menu.style.top = top + 'px';
  menu.style.left = left + 'px';
  menu.style.minWidth = rect.width + 'px';
}

function _tofuDdOpen(wrap) {
  var select = _tofuDdSelectOf(wrap);
  if (!select || select.disabled) return;
  if (_TOFU_DD_OPEN === wrap) { _tofuDdClose(); return; }
  _tofuDdClose();

  var options = _tofuDdOptions(select);
  var menu = document.createElement('div');
  menu.id = 'tofuDdMenu';
  menu.className = 'tofu-dd-menu';
  menu.setAttribute('role', 'listbox');
  menu.setAttribute('aria-label', select.getAttribute('aria-label') || '');

  var activeEntry = null;
  options.forEach(function(opt) {
    var entry = document.createElement('button');
    entry.type = 'button';
    entry.className = 'tofu-dd-item' + (opt.selected ? ' active' : '');
    entry.setAttribute('role', 'option');
    entry.setAttribute('aria-selected', opt.selected ? 'true' : 'false');
    entry.dataset.ddIndex = String(opt.index);
    entry.disabled = opt.disabled;
    var label = document.createElement('span');
    label.className = 'tofu-dd-item-label';
    label.textContent = opt.label;
    entry.appendChild(label);
    var check = document.createElement('span');
    check.className = 'tofu-dd-item-check';
    check.setAttribute('aria-hidden', 'true');
    check.textContent = '✓';
    entry.appendChild(check);
    entry.addEventListener('click', function() {
      _tofuDdChoose(wrap, opt.index);
    });
    menu.appendChild(entry);
    if (opt.selected) activeEntry = entry;
  });

  document.body.appendChild(menu);
  wrap.classList.add('open');
  var trigger = wrap.querySelector('.tofu-dd-trigger');
  if (trigger) trigger.setAttribute('aria-expanded', 'true');
  _TOFU_DD_OPEN = wrap;
  _tofuDdPosition(wrap, menu);
  if (activeEntry) activeEntry.focus({ preventScroll: true });

  /* Close on outside interaction. A scroll elsewhere detaches a body-anchored
   * menu from its trigger, so close rather than float (same rule as the
   * composer provider menu). Capture so inner-menu scrolling isn't caught. */
  var onDocPointer = function(event) {
    if (!menu.contains(event.target) && !wrap.contains(event.target)) _tofuDdClose();
  };
  var onScroll = function(event) {
    if (!menu.contains(event.target)) _tofuDdClose();
  };
  var onKey = function(event) {
    if (event.key === 'Escape') {
      event.stopPropagation();
      _tofuDdClose();
      var tr = wrap.querySelector('.tofu-dd-trigger');
      if (tr) tr.focus({ preventScroll: true });
    }
  };
  /* Attach the outside-interaction listeners on the NEXT tick. The opening
   * gesture (trigger click / Enter) itself produces trailing pointerdown/click
   * events in the same task; a synchronously-attached capture listener would
   * immediately see that same gesture and close the menu it just opened — the
   * exact bug image-gen.js:240 guards against with setTimeout(0). Escape and
   * item-clicks are handled on the menu/entries directly, so deferring the
   * document-level outside-close is safe. */
  var attached = false;
  var attach = function() {
    if (attached || !menu.isConnected) return;
    attached = true;
    document.addEventListener('pointerdown', onDocPointer, true);
    document.addEventListener('scroll', onScroll, true);
  };
  setTimeout(attach, 0);
  document.addEventListener('keydown', onKey, true);
  /* Stash teardown on the menu so close/remove detaches listeners. */
  menu._tofuDdTeardown = function() {
    attached = true; /* neutralize a pending deferred attach */
    document.removeEventListener('pointerdown', onDocPointer, true);
    document.removeEventListener('scroll', onScroll, true);
    document.removeEventListener('keydown', onKey, true);
  };
}

function _tofuDdChoose(wrap, index) {
  var select = _tofuDdSelectOf(wrap);
  if (!select) { _tofuDdClose(); return; }
  var opt = select.options[index];
  if (!opt || opt.disabled) { _tofuDdClose(); return; }
  if (select.selectedIndex !== index) {
    select.selectedIndex = index;
    /* Fire the same bubbling change the native popup would, so the delegated
     * data-tofu-action-change handler resolves against the select element. */
    select.dispatchEvent(new Event('change', { bubbles: true }));
  }
  _tofuDdSyncTrigger(wrap, select);
  _tofuDdClose();
  var trigger = wrap.querySelector('.tofu-dd-trigger');
  if (trigger) trigger.focus({ preventScroll: true });
}

function _tofuDdSyncTrigger(wrap, select) {
  var label = wrap.querySelector('.tofu-dd-value');
  if (!label) return;
  var opt = select.options[select.selectedIndex];
  label.textContent = opt ? (opt.textContent || opt.value) : '';
  var placeholder = select.getAttribute('data-dd-placeholder');
  if (!opt && placeholder) {
    label.textContent = placeholder;
    label.classList.add('is-placeholder');
  } else {
    label.classList.remove('is-placeholder');
  }
}

/* Enhance a single select (idempotent): wrap it, hide the native control,
 * and insert the styled trigger after it. */
function _tofuDdEnhanceOne(select) {
  if (!select || select._tofuDdEnhanced) {
    /* Already enhanced — just resync the visible value (options may have
     * been rebuilt underneath by a re-render). */
    if (select && select._tofuDdWrap) _tofuDdSyncTrigger(select._tofuDdWrap, select);
    return;
  }
  var wrap = document.createElement('span');
  wrap.className = 'tofu-dd';
  select.parentNode.insertBefore(wrap, select);
  wrap.appendChild(select);
  select.classList.add('tofu-dd-native');
  select.setAttribute('aria-hidden', 'false'); // keep it in the a11y tree
  select.tabIndex = -1; // trigger takes keyboard focus instead

  var trigger = document.createElement('button');
  trigger.type = 'button';
  trigger.className = 'tofu-dd-trigger';
  trigger.setAttribute('aria-haspopup', 'listbox');
  trigger.setAttribute('aria-expanded', 'false');
  if (select.disabled) trigger.disabled = true;
  var value = document.createElement('span');
  value.className = 'tofu-dd-value';
  trigger.appendChild(value);
  var arrow = document.createElement('span');
  arrow.className = 'tofu-dd-arrow';
  arrow.setAttribute('aria-hidden', 'true');
  trigger.appendChild(arrow);
  trigger.addEventListener('click', function(event) {
    event.preventDefault();
    event.stopPropagation();
    _tofuDdOpen(wrap);
  });
  trigger.addEventListener('keydown', function(event) {
    if (event.key === 'ArrowDown' || event.key === 'Enter' || event.key === ' ') {
      event.preventDefault();
      _tofuDdOpen(wrap);
    }
  });
  wrap.appendChild(trigger);

  select._tofuDdEnhanced = true;
  select._tofuDdWrap = wrap;
  _tofuDdSyncTrigger(wrap, select);
}

/* Re-entrant entry point: enhance every opted-in select under root. Called
 * after each settings re-render; default root is the whole document so
 * callers don't have to thread the node through. The trigger and menu use
 * direct listeners (not data-tofu-action) because the menu is body-anchored
 * and ephemeral; only the underlying select keeps its declarative change
 * action, which is the contract existing handlers rely on. */
function _tofuDdEnhanceAll(root) {
  var scope = root || document;
  var selects = scope.querySelectorAll('select[data-tofu-dd], select.tofu-dd-native');
  Array.from(selects).forEach(_tofuDdEnhanceOne);
}

runtimeScope.TofuDropdown = { enhance: _tofuDdEnhanceAll, close: _tofuDdClose };
/* ===== migrated source: settings/provider_render.js ===== */
/*
 * 服务商 (Providers) settings tab — provider supply for model-routing v2.
 *
 * Responsibility: everything rendered into ``#settingsTab_api``
 * (static/settings_panels/api.html): ProviderAccess cards with in-card
 * Connection/Credential editors, staged provider metadata edits, and the
 * add-model pickers. Per-offering supply (enable/alias/remove) is managed
 * inline in the per-provider access matrix (settings/access_matrix.js).
 * This file never renders the 模型 (Models) tab — the global, read-only
 * Creator/Model registry lives in settings/model_catalog_tab.js; flows
 * that reload the authority or change creators/models repaint both pages
 * by name via ``_renderModelRoutingTabs()``. Legacy editors are migration
 * input only.
 */

function _setModelRoutingCollectionField(collection, index, field, value, kind) {
  if (!_stgModelRouting || !Array.isArray(_stgModelRouting[collection])) return;
  var row = _stgModelRouting[collection][index];
  if (!row) return;
  if (kind === 'boolean') row[field] = !!value;
  else if (kind === 'number') row[field] = Math.max(0, Number(value) || 0);
  else row[field] = String(value == null ? '' : value);
  _renderProvidersTab();
}

function _modelRoutingPriceLabel(pricing) {
  if (!pricing) return '未设置成交价';
  return (pricing.currency || 'USD') + ' ' + Number(pricing.input || 0) + ' / ' +
    Number(pricing.output || 0) + ' · 每百万 tokens';
}

var _MODEL_CAP_LABELS = {
  text: '文本', thinking: '思考', vision: '视觉',
  cheap: '低价', image_gen: '图像', embedding: '向量',
};

// 上下文窗口按惯例缩写：1000000 → 1M，128000 → 128K，8192 → 8.2K。
function _modelRoutingContextLabel(tokens) {
  var n = Number(tokens) || 0;
  if (n >= 1000000) {
    var m = n / 1000000;
    return (m % 1 === 0 ? String(m) : String(Math.round(m * 10) / 10)) + 'M';
  }
  if (n >= 1000) {
    var k = n / 1000;
    return (k % 1 === 0 ? String(k) : String(Math.round(k * 10) / 10)) + 'K';
  }
  return String(n);
}
function _modelRoutingRefLabel(offering, modelNames) {
  if (offering.identity_state === 'pending_identity') {
    return offering.pending_model_id || offering.offering_id;
  }
  var ref = offering.model || {};
  var key = (ref.creator_id || '') + '::' + (ref.model_id || '');
  return modelNames[key] || ref.model_id || offering.offering_id;
}

function _modelRoutingHasProviderBundle(bundle) {
  if (!_stgModelRouting || !bundle || !bundle.provider) return false;
  var providerId = bundle.provider.provider_id;
  var connectionUrls = new Set((bundle.connections || []).map(function(row) {
    return String(row.base_url || '').replace(/\/$/, '');
  }));
  if ((_stgModelRouting.providers || []).some(function(row) {
    return row.provider_id === providerId;
  })) return true;
  return (_stgModelRouting.connections || []).some(function(row) {
    return connectionUrls.has(String(row.base_url || '').replace(/\/$/, ''));
  });
}

async function _stageModelRoutingProviderBundle(bundle, apiKey) {
  if (!_stgModelRouting || !bundle || !bundle.provider) return false;
  if (_modelRoutingHasProviderBundle(bundle)) {
    showAlert('该服务商或接入点已经存在，请直接编辑现有接入配置。');
    return false;
  }
  var draft = JSON.parse(JSON.stringify(bundle));
  /* Send the compiled references intact. The server inserts missing Models
   * and preserves existing Model settings; provider claims remain scoped to
   * the Offering and are validated against the current Model authority. */
  var extraHeaders = bundle.credential_extra_headers || {};
  /* Static recipe headers are inspectable configuration: they belong on
   * each connection's extra_headers (the 自定义请求头 editor), not folded
   * into the encrypted credential envelope. */
  (draft.connections || []).forEach(function(connection) {
    connection.extra_headers = Object.assign(
      {}, connection.extra_headers || {}, extraHeaders);
  });
  // The compile-only mirror field is not part of ProviderWriteRequest.
  delete draft.credential_extra_headers;
  var secretCredential = (draft.credentials || []).find(function(row) {
    return row.kind !== 'local_identity';
  });
  if (apiKey && secretCredential) {
    draft.credential_secrets = {};
    draft.credential_secrets[secretCredential.credential_id] = JSON.stringify({
      format: 'tofu.credential-secret/v1',
      api_key: apiKey,
      oauth: '',
      extra_headers: {},
    });
  } else {
    /* Keyless add: a non-local credential without a secret_reference is
     * rejected by the v2 domain, so drop the recipe's placeholder credential;
     * the card's 「+ 添加密钥」 mints a real one with full model grants. */
    draft.credentials = (draft.credentials || []).filter(function(row) {
      return row.kind === 'local_identity';
    });
  }
  var created = await Api.modelRouting.createProvider(
    draft, _stgModelRoutingRevision);
  if (!created || !created.provider) throw new Error('服务商接入未能保存');
  _stgModelRoutingRevision = Number(created.revision || _stgModelRoutingRevision);
  await _loadModelRoutingAuthority();
  _renderModelRoutingTabs();
  var card = Array.from(document.querySelectorAll('.stg-provider-card-v2')).find(function(candidate) {
    return candidate.dataset.providerId === String(bundle.provider.provider_id);
  });
  if (card) {
    card.open = true;
    card.scrollIntoView({ behavior: 'smooth', block: 'start' });
  }
  showToast(t(apiKey ? 'providerCatalog.addedWithKey' : 'providerCatalog.addedWithoutKey'));
  return true;
}

function _showTemplateMenu(btn) {
  runtimeScope._toggleProviderTemplatePicker(btn, _addTemplateProvider, _stageModelRoutingProviderBundle);
}

async function _addTemplateProvider(templateKey) {
  /* Templates add directly: the resulting provider card exposes every
   * editable item (keys, headers, base URL, model supply), so no wizard.
   * The key lands via the card's 「+ 添加密钥」; compile with no
   * selected_model_ids returns the full recipe set. */
  try {
    var result = await Api.providers.compileTemplate(templateKey);
    if (!result || !result.provider_bundle) throw new Error('模板编译未返回接入配置');
    await _stageModelRoutingProviderBundle(result.provider_bundle, '');
  } catch (error) {
    showAlert('添加模板失败：' + String(error && error.message || error));
  }
}
async function addProvider() {
  var baseUrl = await showPrompt(
    '输入 OpenAI 兼容 API Base URL；Tofu 会先探测模型，再创建 v2 接入配置。',
    { placeholder: 'https://api.example.com/v1', title: '添加自定义服务商' });
  if (!baseUrl) return;
  var apiKey = await showPrompt(
    '输入 API Key（本地无密钥服务可留空）',
    { title: '接入凭证' });
  if (apiKey == null) return;
  try {
    var result = await Api.providers.probe(String(baseUrl).trim(), String(apiKey).trim(), '');
    if (!result || !result.provider_bundle) {
      throw new Error(result && (result.error || result.message) || '接入点探测失败');
    }
    await _stageModelRoutingProviderBundle(result.provider_bundle, String(apiKey).trim());
  } catch (error) {
    showAlert('添加自定义服务商失败：' + String(error && error.message || error));
  }
}

function _modelRoutingProviderContext(providerId) {
  if (!_stgModelRouting) return null;
  var documentValue = _stgModelRouting;
  var provider = (documentValue.providers || []).find(function(row) {
    return row.provider_id === providerId;
  });
  var accessIndex = (documentValue.provider_accesses || []).findIndex(function(row) {
    return row.provider_id === providerId;
  });
  var access = (documentValue.provider_accesses || [])[accessIndex];
  if (!provider || !access) return null;
  var accessId = access.provider_access_id;
  var connections = (documentValue.connections || []).map(function(row, index) {
    return { row: row, index: index };
  }).filter(function(item) { return item.row.provider_access_id === accessId; });
  var credentials = (documentValue.credentials || []).map(function(row, index) {
    return { row: row, index: index };
  }).filter(function(item) { return item.row.provider_access_id === accessId; });
  var offerings = (documentValue.offerings || []).map(function(row, index) {
    return { row: row, index: index };
  }).filter(function(item) { return item.row.provider_access_id === accessId; });
  var offeringIds = new Set(offerings.map(function(item) { return item.row.offering_id; }));
  var deployments = (documentValue.deployments || []).map(function(row, index) {
    return { row: row, index: index };
  }).filter(function(item) { return offeringIds.has(item.row.offering_id); });
  var modelNames = {};
  (documentValue.models || []).forEach(function(model) {
    modelNames[(model.creator_id || '') + '::' + (model.model_id || '')] =
      model.display_name || model.model_id;
  });
  var modelReleaseDates = {};
  (documentValue.models || []).forEach(function(model) {
    if (model.release_date) {
      modelReleaseDates[(model.creator_id || '') + '::' + (model.model_id || '')] =
        model.release_date;
    }
  });
  var modelListPricing = {};
  (documentValue.models || []).forEach(function(model) {
    if (model.list_pricing) {
      modelListPricing[(model.creator_id || '') + '::' + (model.model_id || '')] =
        model.list_pricing;
    }
  });
  var modelLifecycles = {};
  (documentValue.models || []).forEach(function(model) {
    if (model.lifecycle) {
      modelLifecycles[(model.creator_id || '') + '::' + (model.model_id || '')] =
        model.lifecycle;
    }
  });
  return {
    provider: provider,
    access: access,
    accessIndex: accessIndex,
    connections: connections,
    credentials: credentials,
    offerings: offerings,
    deployments: deployments,
    modelReleaseDates: modelReleaseDates,
    modelListPricing: modelListPricing,
    modelLifecycles: modelLifecycles,
    modelNames: modelNames,
  };
}

function _modelRoutingProviderBrand(context) {
  var provider = context.provider;
  var evidence = [provider.name, provider.provider_id].concat(
    context.connections.map(function(item) { return item.row.base_url; })).join(' ');
  var brand = provider.brand === 'oauth'
    ? _detectBrand(evidence)
    : (provider.brand || _detectBrand(evidence));
  if (provider.brand === 'oauth' && brand === 'generic') {
    brand = /codex|chatgpt|openai/i.test(evidence) ? 'openai' :
      (/claude|anthropic/i.test(evidence) ? 'claude' : brand);
  }
  return brand;
}

function _renderModelRoutingProvidersTab(list) {
  if (!_stgModelRouting) {
    list.innerHTML = '<p class="stg-empty">' + escapeHtml(
      _stgModelRoutingLoadError
        ? '加载模型路由失败：' + _stgModelRoutingLoadError
        : '正在加载服务商接入配置…') + '</p>';
    return;
  }
  var document = _stgModelRouting;
  var providers = document.providers || [];
  if (!providers.length) {
    list.innerHTML = '<p class="stg-empty">尚未配置服务商。</p>';
    return;
  }

  // Refreshed in place so polling never disturbs an open card.
  var expandedIds = new Set();
  if (typeof list.querySelectorAll === 'function') {
    list.querySelectorAll('details.stg-provider-card-v2[open]').forEach(function(card) {
      expandedIds.add(String(card.getAttribute('data-provider-id') || ''));
    });
  }
  var html = '';
  providers.forEach(function(provider) {
    var context = _modelRoutingProviderContext(provider.provider_id);
    if (!context) return;
    var access = context.access;
    var modelCount = new Set(context.offerings.filter(function(item) {
      return item.row.identity_state === 'confirmed' && !!item.row.model;
    }).map(function(item) {
      var row = item.row;
      return row.model.creator_id + '::' + row.model.model_id;
    })).size;
    var providerBrand = _modelRoutingProviderBrand(context);
    // Classic card head: base_url subtitle + credential/model badges; the red
    // off badge is the only state chip, shown only when disabled.
    var primaryConnection = context.connections.find(function(item) {
      return item.row.enabled !== false && item.row.base_url;
    }) || context.connections.find(function(item) { return item.row.base_url; });
    var headSubtitle = primaryConnection ? primaryConnection.row.base_url : provider.provider_id;
    html += '<details class="stg-provider-card stg-provider-card-v2" data-provider-id="' +
      escapeHtml(provider.provider_id) + '"' +
      (expandedIds.has(String(provider.provider_id)) ? ' open' : '') + '>' +
      '<summary class="stg-provider-head stg-provider-head-v2">' +
        '<div class="stg-provider-icon">' + _brandSvg(providerBrand, 22) + '</div>' +
        '<div class="stg-provider-info"><div class="stg-provider-name">' +
          escapeHtml(access.display_name || provider.name || provider.provider_id) + '</div>' +
          '<div class="stg-provider-url">' + escapeHtml(headSubtitle) + '</div></div>' +
        '<div class="stg-provider-badges">' +
          '<span class="stg-badge">' + context.credentials.length + ' 个凭证</span>' +
          '<span class="stg-badge stg-badge-models">' + modelCount + ' 个模型</span>' +
          (access.enabled ? '' : '<span class="stg-badge off">已停用</span>') + '</div>' +
        '<span class="stg-chevron">▾</span>' +
      '</summary>' +
      '<div class="stg-provider-body">' +
        '<div class="stg-field-grid">' +
          '<div class="stg-field"><label>显示名称</label>' +
            '<input type="text" value="' + escapeHtml(access.display_name || provider.name || '') + '" ' +
            'data-tofu-action-change="_setModelRoutingCollectionField(\'provider_accesses\',' +
            context.accessIndex + ',\'display_name\',this.value,\'string\')"></div>' +
          (primaryConnection ? '<div class="stg-field"><label>API 地址 (Base URL)</label>' +
            '<input type="text" value="' + escapeHtml(primaryConnection.row.base_url) + '" ' +
            'data-tofu-action-change="_setModelRoutingCollectionField(\'connections\',' +
            primaryConnection.index + ',\'base_url\',this.value,\'string\')"></div>' : '') +
        '</div>' +
      _renderV2KeysSection(provider, context) +
      (primaryConnection ? _renderV2HeadersSection(primaryConnection) : '') +
      '<div class="stg-provider-foot">' +
      (primaryConnection ? _renderV2ThinkingFormatField(primaryConnection) : '') +
      '<div class="stg-provider-foot-actions">' +
        '<div class="stg-toggle-row"><span>启用</span>' +
          '<label class="stg-toggle"><input type="checkbox"' + (access.enabled ? ' checked' : '') +
          ' data-tofu-action-change="_setModelRoutingCollectionField(\'provider_accesses\',' +
          context.accessIndex + ',\'enabled\',this.checked,\'boolean\')">' +
          '<span class="stg-toggle-track"><span class="stg-toggle-thumb"></span></span></label>' +
        '</div>' +
        '<button type="button" class="stg-btn-danger" data-provider-id="' +
          escapeHtml(provider.provider_id) + '" ' +
          'data-tofu-action="_deleteModelRoutingProvider(this.dataset.providerId)">删除服务商</button>' +
      '</div></div>' +
      '<div class="stg-models-section">' + _renderAccessMatrix(provider.provider_id) + '</div>' +
      '</div>' +
      '</details>';
  });
  list.innerHTML = html;
  runtimeScope.mountProviderReasoningEditors?.(list, () => _stgModelRouting);
  runtimeScope.mountRequestNameEditors?.(list, {
    document: () => _stgModelRouting,
    prompt: (name) => showPrompt(t('settings.requestNameEdit'), { defaultValue: name }),
    alert: (message) => showAlert(message), render: () => _renderProvidersTab(),
  });
  /* Enhance the footer 思维参数格式 select into the unified tofu dropdown so
   * its popup matches the composer popovers. The select stays the change
   * authority; only the popup is rendered as DOM. */
  if (runtimeScope.TofuDropdown) runtimeScope.TofuDropdown.enhance(list);
  _loadV2KeyStats();
}

/* ── Classic provider-card sections (v0.15.0 look, v2 data) ──
 * Key cards render only the server-held head…tail hint; plaintext enters
 * the DOM solely through the eye toggle, which reads it back from the
 * audited reveal endpoint on a deliberate click and drops it again on
 * hide or re-render. Adding/deleting a key goes through saveProvider
 * immediately because only the server can mint/clean secret references;
 * every other edit here is staged locally and persisted by the global 保存.
 */

var _V2_KEY_EYE_OPEN = '<svg viewBox="0 0 16 16" width="13" height="13" fill="none" ' +
  'stroke="currentColor" stroke-width="1.4" stroke-linecap="round" stroke-linejoin="round" ' +
  'aria-hidden="true"><path d="M1.6 8S4.1 3.8 8 3.8 14.4 8 14.4 8 11.9 12.2 8 12.2 1.6 8 1.6 8z"/>' +
  '<circle cx="8" cy="8" r="1.9"/></svg>';
var _V2_KEY_EYE_OFF = '<svg viewBox="0 0 16 16" width="13" height="13" fill="none" ' +
  'stroke="currentColor" stroke-width="1.4" stroke-linecap="round" stroke-linejoin="round" ' +
  'aria-hidden="true"><path d="M1.6 8S4.1 3.8 8 3.8 14.4 8 14.4 8 11.9 12.2 8 12.2 1.6 8 1.6 8z"/>' +
  '<circle cx="8" cy="8" r="1.9"/><path d="M2.5 13.5 13.5 2.5"/></svg>';

function _renderV2KeysSection(provider, context) {
  var subscriptionOnly = context.credentials.length > 0 && context.credentials.every(function(item) {
    return item.row.kind === 'oauth' || item.row.kind === 'subscription';
  });
  var canAdd = !subscriptionOnly && context.connections.length > 0;
  var hint = '加密保存在服务端；默认只显示首尾识别位，点击眼睛图标查看明文';
  var html = '<div class="stg-field stg-keys-field" data-provider-id="' +
    escapeHtml(provider.provider_id) + '">' +
    '<div class="stg-keys-header">' +
      '<label style="margin:0;">API 密钥' +
        ' <span class="stg-keys-info" tabindex="0" role="tooltip" aria-label="' + hint +
        '" title="' + hint + '">i</span></label>' +
      (canAdd ? '<button type="button" class="stg-btn-add stg-keys-tb" data-provider-id="' +
        escapeHtml(provider.provider_id) + '" ' +
        'data-tofu-action="_startNewV2ApiKey(this.dataset.providerId)" title="新增一个 API 密钥">+ 添加密钥</button>' : '') +
    '</div>';
  if (!context.credentials.length) {
    html += '<div class="stg-keys-empty">暂无 API 密钥。点击右上角 + 添加。</div>';
  } else {
    html += '<div class="stg-keys-list">' + context.credentials.map(function(item, order) {
      return _renderV2KeyCard(provider, item, order);
    }).join('') + '</div>';
  }
  return html + '</div>';
}

function _renderV2KeyCard(provider, item, order) {
  var row = item.row;
  var keyHint = String(row.key_hint || '').replace(/^…+/, '');
  var credentialId = String(row.credential_id || '');
  var isApiKey = row.kind === 'api_key';
  var display = isApiKey
    ? (keyHint || '••••••••')
    : (row.kind === 'local_identity' ? '本地身份（无需密钥）' : '订阅授权（OAuth）');
  return '<div class="stg-key-card ' + _v2KeyCardStateClass(provider.provider_id, item) +
    '" data-credential-index="' + item.index + '"' +
    ' data-provider-id="' + escapeHtml(provider.provider_id) + '"' +
    ' data-credential-id="' + escapeHtml(credentialId) + '">' +
    '<div class="stg-key-card-edit">' +
      '<span class="stg-keys-idx">#' + (order + 1) + '</span>' +
      '<input class="stg-keys-input" type="text" readonly spellcheck="false" autocomplete="off" ' +
        'value="' + escapeHtml(display) + '" placeholder="sk-…" ' +
        'data-masked="' + escapeHtml(display) + '" ' +
        'title="密文保存在服务端，默认只显示首尾识别位">' +
      (isApiKey && credentialId
        ? '<button type="button" class="stg-keys-btn stg-key-reveal" ' +
          'data-tofu-action="_toggleV2KeyReveal(this)" aria-pressed="false" ' +
          'title="' + escapeHtml(t('settings.showHideKeyTitle')) + '">' +
          _V2_KEY_EYE_OPEN + '</button>'
        : '') +
      '<button type="button" class="stg-keys-btn danger" data-provider-id="' +
        escapeHtml(provider.provider_id) + '" data-credential-index="' + item.index + '" ' +
        'data-tofu-action="_deleteV2Credential(this.dataset.providerId,Number(this.dataset.credentialIndex))" ' +
        'title="删除该密钥">✕</button>' +
    '</div>' +
    '<div class="stg-key-card-stats">' + _renderV2KeyCardStats(provider.provider_id, item) + '</div>' +
  '</div>';
}

function _v2KeyDisplaySecret(secret) {
  /* Credentials carrying encrypted extra_headers store a
   * tofu.credential-secret/v1 envelope; the key card reveals only the
   * usable key, never the envelope JSON chrome. */
  try {
    var decoded = JSON.parse(secret);
    if (decoded && decoded.format === 'tofu.credential-secret/v1') {
      var apiKey = String(decoded.api_key || '').trim();
      if (apiKey) return apiKey;
    }
  } catch (error) { /* raw key secrets are not JSON */ }
  return secret;
}

function _toggleV2KeyReveal(button) {
  if (!button || typeof button.closest !== 'function') return;
  var card = button.closest('.stg-key-card');
  var input = card ? card.querySelector('.stg-keys-input') : null;
  if (!input) return;
  if (button.getAttribute('aria-pressed') === 'true') {
    input.value = input.getAttribute('data-masked') || '';
    button.setAttribute('aria-pressed', 'false');
    button.innerHTML = _V2_KEY_EYE_OPEN;
    return;
  }
  var credentialId = card.getAttribute('data-credential-id') || '';
  if (!credentialId || typeof Api === 'undefined' || !Api.modelRouting ||
      !Api.modelRouting.revealCredentialSecret) return;
  button.disabled = true;
  Api.modelRouting.revealCredentialSecret(credentialId).then(function(data) {
    button.disabled = false;
    if (!data || typeof data.secret !== 'string' || !data.secret) {
      showToast(t('settings.keyRevealFailed'));
      return;
    }
    var display = _v2KeyDisplaySecret(data.secret);
    input.value = display;
    button.setAttribute('aria-pressed', 'true');
    button.innerHTML = _V2_KEY_EYE_OFF;
  });
}

/* ── Per-key runtime stats (today) — classic two-row key card ──
 * Source: GET /api/v4/dispatch/key-stats — daily per-credential health
 * keyed by credential_id under the owner-scoped provider namespace
 * (slot.key_stats_provider_id). The card toggle posts an immediate
 * runtime override valid for today only; a durable-disabled credential
 * (v2 document enabled=false) can only be resurrected through the staged
 * document field, because the dispatcher never mints a slot for it.
 */

var _v2KeyStatsCache = null;
var _v2KeyStatsLoading = false;

function _loadV2KeyStats() {
  if (_v2KeyStatsLoading || _v2KeyStatsCache) return;
  if (typeof Api === 'undefined' || !Api.dispatch || !Api.dispatch.keyStats) return;
  _v2KeyStatsLoading = true;
  Api.dispatch.keyStats()
    .then(function(data) {
      _v2KeyStatsCache = (data && typeof data === 'object') ? data : { providers: {} };
    })
    .catch(function() {
      _v2KeyStatsCache = { providers: {} };
    })
    .finally(function() {
      _v2KeyStatsLoading = false;
      _refreshV2KeyStatsDom();
    });
}

function _v2KeyStatsBucket(providerId, credentialId) {
  var providers = (_v2KeyStatsCache && _v2KeyStatsCache.providers) || {};
  var candidates = [];
  if (providers[providerId]) candidates.push(providerId);
  var suffix = ':' + providerId;
  var names = Object.keys(providers);
  for (var i = 0; i < names.length; i++) {
    if (names[i] !== providerId && names[i].slice(-suffix.length) === suffix) {
      candidates.push(names[i]);
    }
  }
  /* A stale legacy bucket (e.g. persistent overrides recorded pre-v2 under
   * the bare provider id) must not shadow the owner-scoped bucket that
   * actually holds this credential's rows: resolve by content first. */
  if (credentialId) {
    for (var j = 0; j < candidates.length; j++) {
      if (providers[candidates[j]][credentialId]) {
        return { id: candidates[j], keys: providers[candidates[j]] };
      }
    }
  }
  return candidates.length
    ? { id: candidates[0], keys: providers[candidates[0]] }
    : null;
}

function _v2KeyStatRow(providerId, credentialId) {
  var bucket = _v2KeyStatsBucket(providerId, credentialId);
  return bucket ? (bucket.keys[credentialId] || null) : null;
}

/* Namespace for override writes: the bucket already holding this
 * credential's rows always wins (it is the exact id the dispatcher
 * recorded under); otherwise compose from the server-reported
 * key_namespace (owner-scoped since routing v2). A legacy bucket with no
 * rows for the credential must never capture the write — the dispatcher
 * would never read an override recorded under it. */
function _v2KeyNamespace(providerId, credentialId) {
  var bucket = _v2KeyStatsBucket(providerId, credentialId);
  if (bucket && credentialId && bucket.keys[credentialId]) return bucket.id;
  var prefix = (_v2KeyStatsCache && _v2KeyStatsCache.key_namespace) || '';
  if (prefix) return prefix + providerId;
  if (bucket) return bucket.id;
  return providerId;
}

function _v2KeyCardStateClass(providerId, item) {
  if (!item.row.enabled) return 'stg-keystat-disabled';
  var row = _v2KeyStatRow(providerId, String(item.row.credential_id || ''));
  if (!row) return 'stg-keystat-idle';
  if (row.exhausted && row.override !== true) return 'stg-keystat-exhausted';
  if (!row.enabled) return 'stg-keystat-disabled';
  if (row.auto_disabled) return 'stg-keystat-warn';
  if (row.success_rate == null) return 'stg-keystat-idle';
  var minRate = (_v2KeyStatsCache && _v2KeyStatsCache.min_success_rate) || 0.5;
  if (row.success_rate >= 0.9) return 'stg-keystat-good';
  if (row.success_rate >= minRate) return 'stg-keystat-ok';
  return 'stg-keystat-warn';
}

function _renderV2KeyCardStats(providerId, item) {
  var credentialId = String(item.row.credential_id || '');
  var row = _v2KeyStatRow(providerId, credentialId);
  var effectiveEnabled = !!item.row.enabled && (row ? !!row.enabled : true);

  var total = row ? (row.total || 0) : 0;
  var succ = row ? (row.success || 0) : 0;
  var fail = row ? (row.failure || 0) : 0;
  var rl429 = row ? (row.rate_limited || 0) : 0;
  var gw = row ? (row.gateway_errors || 0) : 0;
  var cons429 = row ? (row.consecutive_429 || 0) : 0;
  var max429 = (_v2KeyStatsCache && _v2KeyStatsCache.max_consecutive_429) || 100;
  var modelStops = row ? Object.keys(row.exhausted_models || {}) : [];
  var srTxt = row && row.success_rate != null
    ? Math.round(row.success_rate * 100) + '%' : '—';

  var badges = '';
  if (row && row.override === false) {
    badges += '<span class="stg-keystat-badge off">' + escapeHtml(t('settings.keyStatOverrideOff')) + '</span>';
  } else if (row && row.override === true) {
    badges += (row.exhausted || modelStops.length)
      ? '<span class="stg-keystat-badge warn" title="' + escapeHtml(t('settings.keyStatOverrideVsExhaustedTip')) + '">' + escapeHtml(t('settings.keyStatOverrideVsExhausted')) + '</span>'
      : '<span class="stg-keystat-badge on">' + escapeHtml(t('settings.keyStatOverrideOn')) + '</span>';
  } else if (row && row.last_resort) {
    badges += '<span class="stg-keystat-badge warn" title="' + escapeHtml(t('settings.keyStatLastResortTip')) + '">' + escapeHtml(t('settings.keyStatLastResort')) + '</span>';
  } else if (row && row.exhausted) {
    badges += '<span class="stg-keystat-badge warn" title="' + escapeHtml(t('settings.keyStatExhaustedTip')) + '">' + escapeHtml(t('settings.keyStatExhausted')) + '</span>';
  } else if (row && row.auto_disabled) {
    badges += '<span class="stg-keystat-badge warn">' + escapeHtml(t('settings.keyStatAutoOff')) + '</span>';
  }
  if (row && !row.exhausted && row.override == null && modelStops.length) {
    var reasons = modelStops.map(function(model) {
      return model + ': ' + ((row.exhausted_models || {})[model] || '');
    }).join('\n');
    badges += '<span class="stg-keystat-badge warn" title="' +
      escapeHtml(t('settings.keyStatModelExhaustedTip', { reasons: reasons })) + '">' +
      escapeHtml(t('settings.keyStatModelExhausted', { models: modelStops.join('、') })) + '</span>';
  }
  if (row && !row.exhausted && cons429 >= Math.max(10, max429 / 2)) {
    badges += '<span class="stg-keystat-badge warn" title="' +
      escapeHtml(t('settings.keyStat429StreakTip')) + '">' +
      escapeHtml(t('settings.keyStat429Streak', { n: cons429 })) + '</span>';
  }
  if (row && row.last_error && (fail > 0 || row.exhausted)) {
    badges += '<span class="stg-keystat-err" title="' + escapeHtml(row.last_error) + '">' +
      escapeHtml(t('settings.keyStatLastError')) + '</span>';
  }

  var metrics = total > 0
    ? '<span class="stg-keystat-metrics">' +
      '<span class="stg-keystat-rate" title="' +
        escapeHtml(t('settings.keyStatRateTip', { succ: succ, total: total })) + '">' +
        srTxt + '</span>' +
      '<span class="stg-keystat-count" title="' + escapeHtml(t('settings.keyStatCountTip')) + '">' +
        escapeHtml(t('settings.keyStatCount', { n: total })) + '</span>' +
      (fail > 0 ? '<span class="stg-keystat-fail" title="' + escapeHtml(t('settings.keyStatFailTip')) + '">' +
        escapeHtml(t('settings.keyStatFail', { n: fail })) + '</span>' : '') +
      (rl429 > 0 ? '<span class="stg-keystat-429" title="' + escapeHtml(t('settings.keyStat429Tip')) + '">' +
        escapeHtml(t('settings.keyStat429', { n: rl429 })) + '</span>' : '') +
      (gw > 0 ? '<span class="stg-keystat-gateway" title="' + escapeHtml(t('settings.keyStatGatewayTip')) + '">' +
        escapeHtml(t('settings.keyStatGateway', { n: gw })) + '</span>' : '') +
      '</span>'
    : '<span class="stg-keystat-none">' + escapeHtml(t('settings.keyStatNoCallsTip')) + '</span>';

  return metrics +
    badges +
    '<span class="stg-keystat-actions">' +
      '<label class="stg-toggle stg-key-toggle" title="' + escapeHtml(t('settings.keyStatToggleTip')) + '">' +
        '<input type="checkbox"' + (effectiveEnabled ? ' checked' : '') +
          ' data-provider-id="' + escapeHtml(providerId) + '"' +
          ' data-credential-id="' + escapeHtml(credentialId) + '"' +
          ' data-tofu-action-change="_onV2KeyToggle(this.dataset.providerId,this.dataset.credentialId,this.checked)">' +
        '<span class="stg-toggle-track"><span class="stg-toggle-thumb"></span></span>' +
      '</label>' +
      (row && row.override != null
        ? '<button type="button" class="stg-btn-link" title="' + escapeHtml(t('settings.keyStatClearOverrideTip')) + '"' +
          ' data-provider-id="' + escapeHtml(providerId) + '"' +
          ' data-credential-id="' + escapeHtml(credentialId) + '"' +
          ' data-tofu-action="_onV2KeyClearOverride(this.dataset.providerId,this.dataset.credentialId)">' +
          escapeHtml(t('settings.keyStatReset')) + '</button>'
        : '') +
    '</span>';
}

function _onV2KeyToggle(providerId, credentialId, enabled) {
  var context = _modelRoutingProviderContext(providerId);
  var item = context ? context.credentials.find(function(candidate) {
    return String(candidate.row.credential_id || '') === String(credentialId);
  }) : null;
  if (item && !item.row.enabled && enabled) {
    // Durable-disabled credential: a runtime override cannot resurrect it
    // (the dispatcher never mints a slot for a disabled credential), so
    // re-enable through the staged document field; the footer 保存 commits.
    _setModelRoutingCollectionField('credentials', item.index, 'enabled', true, 'boolean');
    showToast('已重新启用 — 保存后生效。');
    return;
  }
  _v2KeyOverride(providerId, credentialId, !!enabled);
}

function _onV2KeyClearOverride(providerId, credentialId) {
  _v2KeyOverride(providerId, credentialId, null);
}

function _v2KeyOverride(providerId, credentialId, enabled) {
  if (typeof Api === 'undefined' || !Api.dispatch || !Api.dispatch.keyOverride) return;
  var namespaced = _v2KeyNamespace(providerId, credentialId);
  Api.dispatch.keyOverride({
    provider_id: namespaced,
    key_name: credentialId,
    enabled: enabled,
  }).then(function(data) {
    if (data && data.row) {
      if (!_v2KeyStatsCache) _v2KeyStatsCache = { providers: {} };
      if (!_v2KeyStatsCache.providers) _v2KeyStatsCache.providers = {};
      if (!_v2KeyStatsCache.providers[namespaced]) _v2KeyStatsCache.providers[namespaced] = {};
      _v2KeyStatsCache.providers[namespaced][credentialId] = data.row;
    } else {
      showToast('密钥切换失败，请稍后重试。');
    }
    _refreshV2KeyStatsDom();
  });
}

function _refreshV2KeyStatsDom() {
  if (typeof document === 'undefined' || typeof document.querySelectorAll !== 'function') return;
  var cards = document.querySelectorAll('.stg-key-card[data-credential-id]');
  for (var i = 0; i < cards.length; i++) {
    var card = cards[i];
    var providerId = card.getAttribute('data-provider-id') || '';
    var credentialId = card.getAttribute('data-credential-id') || '';
    var context = _modelRoutingProviderContext(providerId);
    var item = context ? context.credentials.find(function(candidate) {
      return String(candidate.row.credential_id || '') === credentialId;
    }) : null;
    if (!item) continue;
    var classes = (card.className || '').split(/\s+/).filter(function(name) {
      return name && name.indexOf('stg-keystat-') !== 0;
    });
    classes.push(_v2KeyCardStateClass(providerId, item));
    card.className = classes.join(' ');
    var statsEl = card.querySelector('.stg-key-card-stats');
    if (statsEl) statsEl.innerHTML = _renderV2KeyCardStats(providerId, item);
  }
}

function _startNewV2ApiKey(providerId) {
  var field = document.querySelector(
    '.stg-keys-field[data-provider-id="' + providerId + '"]');
  if (!field) return;
  var existing = field.querySelector('.stg-key-card--new input');
  if (existing) { existing.focus(); return; }
  var list = field.querySelector('.stg-keys-list');
  if (!list) {
    var emptyEl = field.querySelector('.stg-keys-empty');
    if (emptyEl) emptyEl.remove();
    list = document.createElement('div');
    list.className = 'stg-keys-list';
    field.appendChild(list);
  }
  var order = list.querySelectorAll('.stg-key-card').length;
  list.insertAdjacentHTML('beforeend',
    '<div class="stg-key-card stg-key-card--blank stg-key-card--new">' +
      '<div class="stg-key-card-edit">' +
        '<span class="stg-keys-idx">#' + (order + 1) + '</span>' +
        '<input class="stg-keys-input" type="text" spellcheck="false" autocomplete="off" placeholder="sk-…">' +
        '<button type="button" class="stg-keys-btn danger" title="取消">✕</button>' +
      '</div>' +
    '</div>');
  var card = list.lastElementChild;
  var input = card.querySelector('input');
  card.querySelector('.stg-keys-btn').onclick = function() { card.remove(); };
  input.addEventListener('keydown', function(event) {
    if (event.key === 'Enter') {
      event.preventDefault();
      void _commitNewV2ApiKey(providerId, card, input);
    }
  });
  input.addEventListener('blur', function() {
    if (input.value.trim()) void _commitNewV2ApiKey(providerId, card, input);
  });
  input.focus();
}

async function _commitNewV2ApiKey(providerId, card, input) {
  var apiKey = input.value.trim();
  if (!apiKey) { input.focus(); return; }
  var context = _modelRoutingProviderContext(providerId);
  if (!context) { card.remove(); return; }
  input.disabled = true;
  try {
    await _saveNewProviderCredential(context, apiKey);
  } catch (error) {
    input.disabled = false;
    input.focus();
  }
}

async function _deleteV2Credential(providerId, credentialIndex) {
  var context = _modelRoutingProviderContext(providerId);
  if (!context) return;
  var row = (_stgModelRouting.credentials || [])[credentialIndex];
  if (!row || row.provider_access_id !== context.access.provider_access_id) return;
  var order = context.credentials.findIndex(function(item) {
    return item.row.credential_id === row.credential_id;
  });
  if (!await showConfirm(
    '删除密钥 #' + (order + 1) + '？删除立即生效，使用该密钥的轮转会自动跳过它。',
    { danger: true })) return;
  var bundle = _providerBundleForSave(context);
  bundle.credentials = bundle.credentials.filter(function(candidate) {
    return candidate.credential_id !== row.credential_id;
  });
  try {
    var saved = await Api.modelRouting.saveProvider(
      context.provider.provider_id, bundle, _stgModelRoutingRevision);
    if (!saved || !saved.provider) throw new Error('密钥未能删除');
    _stgModelRoutingRevision = Number(saved.revision || _stgModelRoutingRevision);
    await _loadModelRoutingAuthority();
    _renderProvidersTab();
    showToast('已删除密钥。');
  } catch (error) {
    showAlert('删除密钥失败：' + String(error && error.message || error));
  }
}

function _renderV2HeadersSection(connection) {
  var headers = connection.row.extra_headers || {};
  var entries = Object.keys(headers).map(function(name) {
    return [name, headers[name] == null ? '' : String(headers[name])];
  });
  var html = '<div class="stg-field stg-hdr-field" data-connection-index="' + connection.index + '">' +
    '<div class="stg-hdr-header">' +
      '<label style="margin:0;">自定义请求头' +
        ' <span class="stg-hint">（可选 — 每行一对，附加到本服务商的所有请求）</span></label>' +
      '<button type="button" class="stg-btn-add stg-hdr-tb" ' +
        'data-tofu-action="_addV2HeaderRow(' + connection.index + ')" title="新增一行请求头">+ 添加请求头</button>' +
    '</div>';
  if (!entries.length) {
    html += '<div class="stg-hdr-empty">暂无自定义请求头。点击右上角 + 添加。</div>';
  } else {
    html += '<div class="stg-hdr-list">' + entries.map(function(entry) {
      return _renderV2HeaderRow(connection.index, entry[0], entry[1]);
    }).join('') + '</div>';
  }
  return html + '</div>';
}

function _renderV2HeaderRow(connectionIndex, name, value) {
  return '<div class="stg-hdr-row">' +
    '<input type="text" class="stg-hdr-name" data-hdr-field="name" placeholder="Header 名称" ' +
      'spellcheck="false" autocomplete="off" value="' + escapeHtml(name || '') + '" ' +
      'data-tofu-action-change="_onV2HeaderRowEdit(' + connectionIndex + ')">' +
    '<span class="stg-hdr-sep">:</span>' +
    '<input type="text" class="stg-hdr-value" data-hdr-field="value" placeholder="Header 值" ' +
      'spellcheck="false" autocomplete="off" value="' + escapeHtml(value || '') + '" ' +
      'data-tofu-action-change="_onV2HeaderRowEdit(' + connectionIndex + ')">' +
    '<button type="button" class="stg-hdr-btn danger" ' +
      'data-tofu-action="_deleteV2HeaderRow(this,' + connectionIndex + ')" title="删除该请求头">✕</button>' +
  '</div>';
}

function _collectV2HeadersFromDom(connectionIndex) {
  var field = document.querySelector(
    '.stg-hdr-field[data-connection-index="' + connectionIndex + '"]');
  if (!field) return null;
  var out = {};
  Array.from(field.querySelectorAll('.stg-hdr-row')).forEach(function(row) {
    var nameEl = row.querySelector('input[data-hdr-field="name"]');
    var valueEl = row.querySelector('input[data-hdr-field="value"]');
    var name = (nameEl && nameEl.value || '').trim();
    if (name) out[name] = valueEl ? valueEl.value : '';
  });
  return out;
}

function _onV2HeaderRowEdit(connectionIndex) {
  if (!_stgModelRouting || !_stgModelRouting.connections[connectionIndex]) return;
  var collected = _collectV2HeadersFromDom(connectionIndex);
  if (collected === null) return;
  _stgModelRouting.connections[connectionIndex].extra_headers = collected;
}

function _addV2HeaderRow(connectionIndex) {
  var field = document.querySelector(
    '.stg-hdr-field[data-connection-index="' + connectionIndex + '"]');
  if (!field) return;
  var list = field.querySelector('.stg-hdr-list');
  if (!list) {
    var emptyEl = field.querySelector('.stg-hdr-empty');
    if (emptyEl) emptyEl.remove();
    list = document.createElement('div');
    list.className = 'stg-hdr-list';
    field.appendChild(list);
  }
  list.insertAdjacentHTML('beforeend', _renderV2HeaderRow(connectionIndex, '', ''));
  var row = list.lastElementChild;
  var nameInput = row && row.querySelector('input[data-hdr-field="name"]');
  if (nameInput) nameInput.focus();
}

function _deleteV2HeaderRow(btn, connectionIndex) {
  var row = btn && btn.closest('.stg-hdr-row');
  if (row) row.remove();
  _onV2HeaderRowEdit(connectionIndex);
  var field = document.querySelector(
    '.stg-hdr-field[data-connection-index="' + connectionIndex + '"]');
  if (field && !field.querySelectorAll('.stg-hdr-row').length) {
    var list = field.querySelector('.stg-hdr-list');
    if (list) list.remove();
    var hint = document.createElement('div');
    hint.className = 'stg-hdr-empty';
    hint.textContent = '暂无自定义请求头。点击右上角 + 添加。';
    field.appendChild(hint);
  }
}

function _renderV2ThinkingFormatField(connection) {
  var value = String(connection.row.thinking_format || '');
  var options = [
    ['', '自动检测（按模型名称）'],
    ['enable_thinking', 'enable_thinking（LongCat/Qwen 风格）'],
    ['thinking_type', 'thinking.type（Doubao/Claude 风格）'],
    ['reasoning_effort', 'reasoning_effort（Gemini 3.x 风格）'],
    ['none', '不发送思维参数'],
  ];
  return '<div class="stg-field stg-thinking-field">' +
    '<label title="默认自动检测 — 仅当端点使用非标准格式时需配置">思维参数格式</label>' +
    '<select data-tofu-dd data-tofu-action-change="_setModelRoutingCollectionField(\'connections\',' +
    connection.index + ',\'thinking_format\',this.value,\'string\')">' +
    options.map(function(option) {
      return '<option value="' + option[0] + '"' + (value === option[0] ? ' selected' : '') +
        '>' + escapeHtml(option[1]) + '</option>';
    }).join('') + '</select></div>';
}

function _removeV2Alias(deploymentIndex) {
  if (!_stgModelRouting) return;
  var deployments = _stgModelRouting.deployments || [];
  var row = deployments[deploymentIndex];
  if (!row) return;
  deployments.splice(deploymentIndex, 1);
  _renderProvidersTab();
}

async function _addV2Alias(providerId, offeringId) {
  var context = _modelRoutingProviderContext(providerId);
  if (!context) return;
  var offering = context.offerings.find(function(item) {
    return item.row.offering_id === offeringId;
  });
  if (!offering) return;
  var alias = await showPrompt(
    t('settings.matrixAddAliasPrompt'),
    { title: '添加请求名', placeholder: 'deepseek-v4-flash-tencent' });
  alias = String(alias || '').trim();
  if (!alias) return;
  var accessOfferingIds = new Set(context.offerings.map(function(item) {
    return item.row.offering_id;
  }));
  var duplicate = (_stgModelRouting.deployments || []).some(function(row) {
    return accessOfferingIds.has(row.offering_id) && row.wire_model_id === alias;
  });
  if (duplicate) {
    showAlert('该请求名已存在于本服务商。');
    return;
  }
  var connection = context.connections.find(function(item) {
    return item.row.enabled !== false;
  }) || context.connections[0];
  if (!connection) return;
  _stgModelRouting.deployments.push({
    deployment_id: 'deployment-' + String(Date.now()).toString(36) + '-' +
      Math.random().toString(36).slice(2, 8),
    offering_id: offeringId,
    connection_id: connection.row.connection_id,
    wire_model_id: alias,
    priority: 100,
    identity_confidence: offering.row.identity_state === 'confirmed' && offering.row.model
      ? 'high' : 'pending',
    probe_status: 'unprobed',
  });
  _renderProvidersTab();
  showToast(t('settings.matrixAliasAdded'));
}

async function _removeV2Offering(providerId, offeringId) {
  var context = _modelRoutingProviderContext(providerId);
  if (!context) return;
  var offering = context.offerings.find(function(item) {
    return item.row.offering_id === offeringId;
  });
  if (!offering) return;
  var label = _modelRoutingRefLabel(offering.row, context.modelNames);
  if (!await showConfirm(
    '从「' + (context.access.display_name || context.provider.name || providerId) +
    '」移除模型「' + label + '」的供给？随全局保存生效。',
    { danger: true })) return;
  _stgModelRouting.offerings = (_stgModelRouting.offerings || []).filter(function(row) {
    return row.offering_id !== offeringId;
  });
  _stgModelRouting.deployments = (_stgModelRouting.deployments || []).filter(function(row) {
    return row.offering_id !== offeringId;
  });
  _renderProvidersTab();
}
/* ── 添加模型 picker：目录多选 + 自定义模型 ──
 * Stages offerings + deployments into _stgModelRouting; the global 保存
 * (Api.modelRouting.replace) persists them. New request names inherit the
 * Offering switch and credential grants; unprobed honestly records that
 * creation has not verified reachability. Confirmed offerings additionally need a
 * credential authorization grant per existing credential (routing refuses
 * ungranted refs), appended here deduped; revoke in the matrix.
 */

var _stgModelPickerProviderId = '';
var _stgModelPickerQuery = '';
var _stgModelPickerChecked = new Set();

function _newV2StagedId(prefix) {
  return prefix + '-' + String(Date.now()).toString(36) + '-' +
    Math.random().toString(36).slice(2, 8);
}

function _providerWireModelIds(context) {
  var ids = new Set();
  (context.deployments || []).forEach(function(item) {
    ids.add(String(item.row.wire_model_id || ''));
  });
  return ids;
}

function _providerAddableCatalogModels(context) {
  var offered = new Set();
  context.offerings.forEach(function(item) {
    var row = item.row;
    if (row.identity_state === 'confirmed' && row.model) {
      offered.add((row.model.creator_id || '') + '::' + (row.model.model_id || ''));
    }
  });
  return (_stgModelRouting.models || []).filter(function(model) {
    if (model.lifecycle === 'retired') return false;
    return !offered.has((model.creator_id || '') + '::' + (model.model_id || ''));
  });
}

async function _stageV2AddModels(providerId, catalogModels, customWireIds) {
  // A new browser bundle can be served before the backend restarts. Do not
  // send optional fields until the current server advertises their contract.
  var authorityCapabilities = await Api.modelRouting.get();
  var supportsInheritance = authorityCapabilities.metadata_inheritance_supported === true;
  var context = _modelRoutingProviderContext(providerId);
  if (!context) return 0;
  var connection = context.connections.find(function(item) {
    return item.row.enabled !== false;
  }) || context.connections[0];
  if (!connection) {
    showAlert('请先为该服务商配置 API 地址，再添加模型。');
    return 0;
  }
  var stagedDocument = _stgModelRouting;
  var requestNames = runtimeScope.planProviderRequestNames
    ? await runtimeScope.planProviderRequestNames(catalogModels || [], context.connections.map(item => item.row.base_url)) : {};
  if (_stgModelRouting !== stagedDocument) return 0;
  context = _modelRoutingProviderContext(providerId);
  if (!context) return 0;
  connection = context.connections.find(item => item.row.connection_id === connection.row.connection_id);
  if (!connection) return 0;
  var wireIds = _providerWireModelIds(context);
  var granted = new Set();
  var added = 0;
  var pushDeployment = function(offeringId, wireId, confidence) {
    _stgModelRouting.deployments.push({
      deployment_id: _newV2StagedId('deployment'),
      offering_id: offeringId,
      connection_id: connection.row.connection_id,
      wire_model_id: wireId,
      identity_confidence: confidence,
      probe_status: 'unprobed',
      priority: 100,
    });
    wireIds.add(wireId);
  };
  var grantModel = function(ref) {
    var grantKey = ref.creator_id + '::' + ref.model_id;
    if (granted.has(grantKey)) return;
    granted.add(grantKey);
    context.credentials.forEach(function(item) {
      var credential = item.row;
      if (!credential.authorization) {
        credential.authorization = { connection_ids: [], models: [] };
      }
      var grants = credential.authorization.models || [];
      var exists = grants.some(function(candidate) {
        return String(candidate.creator_id || '') === String(ref.creator_id) &&
          String(candidate.model_id || '') === String(ref.model_id);
      });
      if (!exists) grants.push({ creator_id: ref.creator_id, model_id: ref.model_id });
      credential.authorization.models = grants;
    });
  };
  (catalogModels || []).forEach(function(model) {
    var names = (requestNames[model.model_id] || [String(model.model_id || '')]).filter(id => id && !wireIds.has(id));
    if (!names.length) return;
    var offeringId = _newV2StagedId('offering');
    _stgModelRouting.offerings.push({
      offering_id: offeringId,
      provider_access_id: context.access.provider_access_id,
      identity_state: 'confirmed',
      model: { creator_id: model.creator_id, model_id: model.model_id },
      enabled: true,
      stale: false,
      capabilities: (model.capabilities && model.capabilities.length
        ? model.capabilities : ['text']).slice(),
      context_window: model.context_window || 128000,
      ...(supportsInheritance ? { inherited_model_fields: ['capabilities', 'context_window'] } : {}),
      priority: 100,
    });
    names.forEach(wireId => pushDeployment(offeringId, wireId, 'high'));
    grantModel({ creator_id: model.creator_id, model_id: model.model_id });
    added++;
  });
  (customWireIds || []).forEach(function(rawId) {
    var wireId = String(rawId || '').trim();
    if (!wireId || wireId.length > 256 || /[\u0000-\u001f]/.test(wireId)) return;
    if (wireIds.has(wireId)) return;
    var offeringId = _newV2StagedId('offering');
    _stgModelRouting.offerings.push({
      offering_id: offeringId,
      provider_access_id: context.access.provider_access_id,
      identity_state: 'pending_identity',
      pending_model_id: wireId,
      enabled: true,
      stale: false,
      capabilities: ['text'],
      context_window: 128000,
      ...(supportsInheritance ? { inherited_model_fields: ['capabilities', 'context_window'] } : {}),
      priority: 100,
    });
    pushDeployment(offeringId, wireId, 'pending');
    added++;
  });
  if (added) {
    _renderProvidersTab();
    // Model adds commit immediately (like key/provider edits): the access
    // matrix probes the SERVER document, so a staged-but-unsaved row would
    // 400 with "no cells match the requested scope".
    await _persistModelRoutingNow();
  }
  return added;
}

function _openProviderModelPicker(providerId) {
  var context = _modelRoutingProviderContext(providerId);
  if (!context) return;
  _closeProviderModelPicker();
  _stgModelPickerProviderId = String(providerId);
  _stgModelPickerQuery = '';
  _stgModelPickerChecked = new Set();
  var overlay = document.createElement('div');
  overlay.id = 'stgProviderModelPickerOverlay';
  overlay.className = 'stg-v2-manager-overlay stg-v2-picker-overlay';
  overlay.setAttribute('role', 'presentation');
  var panel = document.createElement('section');
  panel.className = 'stg-v2-manager stg-v2-picker';
  panel.setAttribute('role', 'dialog');
  panel.setAttribute('aria-modal', 'true');
  panel.setAttribute('aria-label', '添加模型');
  panel.innerHTML = '<header class="stg-v2-manager-head"><div class="stg-v2-manager-brand">' +
    _brandSvg(_modelRoutingProviderBrand(context), 22) + '<div><strong>' +
    escapeHtml(context.access.display_name || context.provider.name || context.provider.provider_id) +
    '</strong><span>添加模型</span></div></div>' +
    '<button type="button" class="stg-v2-close" data-tofu-action="_closeProviderModelPicker()" aria-label="关闭">×</button></header>' +
    '<div class="stg-v2-manager-body">' +
      '<div class="stg-v2-picker-custom"><label>自定义模型 ID</label>' +
        '<div class="stg-v2-picker-custom-row">' +
          '<input id="stgProviderModelCustomId" type="text" spellcheck="false" autocomplete="off" ' +
            'placeholder="发给服务商的模型 ID，例如 deepseek-v4-flash-tencent">' +
          '<button type="button" class="stg-btn-add" data-tofu-action="_stageV2CustomModelFromPicker()">添加</button>' +
        '</div>' +
        '<p class="stg-v2-picker-hint">' + escapeHtml(t('settings.requestNamePendingIdentity')) + '</p></div>' +
      '<div class="stg-v2-picker-catalog"><label>从模型目录选择</label>' +
        '<input class="stg-v2-picker-search" type="search" placeholder="搜索模型名称或 ID" ' +
          'data-tofu-action-input="_filterProviderModelPicker(this.value)">' +
        '<div class="stg-v2-picker-list" id="stgProviderModelPickerList"></div>' +
        '<div class="stg-v2-picker-foot"><span id="stgProviderModelPickerCount">未选择模型</span>' +
          '<button type="button" class="stg-btn-add" data-tofu-action="_stageV2CheckedCatalogModels()">添加所选</button></div>' +
      '</div>' +
    '</div>';
  overlay.appendChild(panel);
  document.body.appendChild(overlay);
  overlay.onclick = function(event) { if (event.target === overlay) _closeProviderModelPicker(); };
  var customInput = panel.querySelector('#stgProviderModelCustomId');
  if (customInput) {
    customInput.addEventListener('keydown', function(event) {
      if (event.key === 'Enter') {
        event.preventDefault();
        _stageV2CustomModelFromPicker();
      }
    });
  }
  _renderProviderModelPickerList();
}

function _closeProviderModelPicker() {
  _stgModelPickerProviderId = '';
  var overlay = document.getElementById('stgProviderModelPickerOverlay');
  if (overlay) overlay.remove();
}

function _filterProviderModelPicker(value) {
  _stgModelPickerQuery = String(value || '');
  _renderProviderModelPickerList();
}

function _toggleProviderModelPick(key, checked) {
  key = String(key || '');
  if (!key) return;
  if (checked) _stgModelPickerChecked.add(key);
  else _stgModelPickerChecked.delete(key);
  _renderProviderModelPickerCount();
}

function _renderProviderModelPickerCount() {
  var count = document.getElementById('stgProviderModelPickerCount');
  if (count) {
    count.textContent = _stgModelPickerChecked.size
      ? '已选择 ' + _stgModelPickerChecked.size + ' 个模型'
      : '未选择模型';
  }
}

function _renderProviderModelPickerList() {
  var list = document.getElementById('stgProviderModelPickerList');
  var context = _modelRoutingProviderContext(_stgModelPickerProviderId);
  if (!list || !context) return;
  var query = _stgModelPickerQuery.trim().toLowerCase();
  var rows = _providerAddableCatalogModels(context).filter(function(model) {
    var haystack = [model.display_name, model.model_id, model.creator_id].join(' ').toLowerCase();
    return !query || haystack.includes(query);
  }).sort(function(left, right) {
    return String(left.display_name || left.model_id).localeCompare(
      String(right.display_name || right.model_id), undefined,
      { numeric: true, sensitivity: 'base' });
  });
  var visible = rows.slice(0, 120);
  list.innerHTML = visible.map(function(model) {
    var key = (model.creator_id || '') + '::' + (model.model_id || '');
    return '<label class="stg-v2-picker-row">' +
      '<input type="checkbox" data-model-key="' + escapeHtml(key) + '"' +
        (_stgModelPickerChecked.has(key) ? ' checked' : '') +
        ' data-tofu-action-change="_toggleProviderModelPick(this.dataset.modelKey,this.checked)">' +
      '<span class="stg-v2-picker-row-icon">' +
        _brandSvg(typeof _modelBrand === 'function'
          ? _modelBrand(model.model_id || '', model.creator_id)
          : _detectBrand((model.creator_id || '') + ' ' + (model.model_id || '')), 16) + '</span>' +
      '<span class="stg-v2-picker-row-id"><strong>' +
        escapeHtml(model.display_name || model.model_id) + '</strong><span>' +
        escapeHtml((model.creator_id || '') + '/' + (model.model_id || '')) + '</span></span>' +
      '<span class="stg-v2-picker-row-price">' +
        escapeHtml(model.list_pricing ? _modelRoutingPriceLabel(model.list_pricing) : '目录未挂牌') +
      '</span></label>';
  }).join('');
  if (!visible.length) {
    list.innerHTML = '<p class="stg-v2-picker-empty">' +
      (rows.length ? '没有匹配的模型。' : '目录中的模型都已添加。') + '</p>';
  }
  _renderProviderModelPickerCount();
}

async function _stageV2CustomModelFromPicker() {
  var input = document.getElementById('stgProviderModelCustomId');
  var wireId = input ? input.value.trim() : '';
  if (!wireId) { if (input) input.focus(); return; }
  var context = _modelRoutingProviderContext(_stgModelPickerProviderId);
  if (!context) return;
  if (wireId.length > 256 || /[\u0000-\u001f]/.test(wireId)) {
    showAlert('模型 ID 无效。');
    return;
  }
  if (_providerWireModelIds(context).has(wireId)) {
    showAlert('该模型 ID 已存在于本服务商。');
    return;
  }
  try {
    if (!await _stageV2AddModels(_stgModelPickerProviderId, [], [wireId])) return;
  } catch (error) {
    showAlert('模型保存失败：' + String(error && error.message || error));
    return;
  }
  input.value = '';
  input.focus();
  _renderProviderModelPickerList();
  showToast('已添加自定义模型。');
}

async function _stageV2CheckedCatalogModels() {
  if (!_stgModelPickerChecked.size) {
    showToast('请先勾选要添加的模型。');
    return;
  }
  var context = _modelRoutingProviderContext(_stgModelPickerProviderId);
  if (!context) return;
  var byKey = {};
  _providerAddableCatalogModels(context).forEach(function(model) {
    byKey[(model.creator_id || '') + '::' + (model.model_id || '')] = model;
  });
  var models = Array.from(_stgModelPickerChecked).map(function(key) {
    return byKey[key];
  }).filter(Boolean);
  var added = 0;
  try {
    added = await _stageV2AddModels(_stgModelPickerProviderId, models, []);
  } catch (error) {
    showAlert('模型保存失败：' + String(error && error.message || error));
  }
  _stgModelPickerChecked = new Set();
  _renderProviderModelPickerList();
  if (added) showToast('已添加 ' + added + ' 个模型。');
}

function _providerBundleForSave(context) {
  return {
    provider: JSON.parse(JSON.stringify(context.provider)),
    provider_access: JSON.parse(JSON.stringify(context.access)),
    connections: context.connections.map(function(item) { return JSON.parse(JSON.stringify(item.row)); }),
    credentials: context.credentials.map(function(item) { return JSON.parse(JSON.stringify(item.row)); }),
    offerings: context.offerings.map(function(item) { return JSON.parse(JSON.stringify(item.row)); }),
    deployments: context.deployments.map(function(item) { return JSON.parse(JSON.stringify(item.row)); }),
    creators: [],
    models: [],
  };
}

async function _deleteModelRoutingProvider(providerId) {
  var context = _modelRoutingProviderContext(providerId);
  if (!context) return;
  var name = context.access.display_name || context.provider.name || providerId;
  if (!await showConfirm('删除服务商「' + name + '」？其凭证、接入点与模型供给会一并移除。',
    { danger: true })) return;
  try {
    var result = await Api.modelRouting.deleteProvider(providerId, _stgModelRoutingRevision);
    if (result && result.revision != null) _stgModelRoutingRevision = Number(result.revision);
    await _loadModelRoutingAuthority();
    _renderModelRoutingTabs();
    showToast('已删除服务商。');
  } catch (error) {
    showAlert('删除服务商失败：' + String(error && error.message || error));
  }
}

async function _saveNewProviderCredential(context, apiKey) {
  var credentialId = 'credential-' + String(Date.now()).toString(36) + '-' +
    Math.random().toString(36).slice(2, 8);
  var modelRefs = [];
  var seen = new Set();
  context.offerings.forEach(function(item) {
    var row = item.row;
    if (row.identity_state !== 'confirmed' || !row.model) return;
    var key = row.model.creator_id + '::' + row.model.model_id;
    if (seen.has(key)) return;
    seen.add(key);
    modelRefs.push(JSON.parse(JSON.stringify(row.model)));
  });
  var bundle = _providerBundleForSave(context);
  bundle.credentials.push({
    credential_id: credentialId,
    provider_access_id: context.access.provider_access_id,
    kind: 'api_key',
    secret_reference: '',
    key_hint: '',
    enabled: true,
    authorization: {
      connection_ids: context.connections.map(function(item) { return item.row.connection_id; }),
      models: modelRefs,
    },
    quota_policy: {},
  });
  bundle.credential_secrets = {};
  bundle.credential_secrets[credentialId] = JSON.stringify({
    format: 'tofu.credential-secret/v1', api_key: String(apiKey).trim(), oauth: '', extra_headers: {},
  });
  try {
    var saved = await Api.modelRouting.saveProvider(
      context.provider.provider_id, bundle, _stgModelRoutingRevision);
    if (!saved || !saved.provider) throw new Error('凭证未能保存');
    _stgModelRoutingRevision = Number(saved.revision || _stgModelRoutingRevision);
    await _loadModelRoutingAuthority();
    _renderProvidersTab();
    showToast('已添加凭证。');
  } catch (error) {
    showAlert('添加凭证失败：' + String(error && error.message || error));
  }
}

/**
 * Repaint ONLY the Providers tab. The 模型 (Models) tab is not a side effect
 * of this function — authority-reloading flows call ``_renderModelRoutingTabs()``
 * (settings/model_catalog_tab.js) to repaint both pages by name.
 */
function _renderProvidersTab() {
  var list = document.getElementById('stgProviderList');
  if (list) _renderModelRoutingProvidersTab(list);
}
/* ===== migrated source: settings/access_matrix.js ===== */
/* ═══════════════════════════════════════════════════════════════════
   settings/access matrix — per-(credential × wire id) capability grid.

   Some gateways (e.g. Meituan AIGC) give each credential a *different*
   quota and a *different* set of accessible models. The flat model list
   can't express that: the v2 authority stores the grant as
   ``credential.authorization.models`` (an allow-list of ModelRefs), which
   is exactly a (credential × model) matrix.

   This module renders that matrix for one ProviderAccess — confirmed
   offerings down the side (one row per upstream wire id, grouped under
   the canonical model), credentials across the top. A cell dot reflects
   the credential's authorization grant; toggling it adds/removes the
   offering's ModelRef in ``_stgModelRouting`` (persisted by the settings
   保存 flow, same as every other v2 card edit).

   ── Probe & Recommend ────────────────────────────────────────────────
   The probe button starts a SERVER-OWNED background task
   (POST /api/v4/providers/<id>/probe-cells/start) that resolves plaintext
   keys from the owner-scoped secret store — the browser never sees them —
   and sends a tiny request to EVERY (credential × wire id) pair, granted
   or not: discovering reachable pairs the allow-list does not grant yet
   is the matrix's whole point on gateways like Meituan. Progress is
   persisted server-side under data/config/probe_cache/, so closing
   Settings (or restarting the server) never loses it — the UI re-attaches
   by provider id and keeps polling. Only "Retest" (force) discards the
   saved result and starts over. Probe updates never edit routing state.
   Explicitly applying recommendations only removes flagged
   (credential × model) grants from the allow-list.

   This file is concatenated by Vite's module graph — symbols share the
   same window scope as every other runtime section. No imports needed.
   ═══════════════════════════════════════════════════════════════════ */

/** Per-provider management-surface state (search text + status filter
 *  chip), keyed by provider_id. The matrix is this provider's always-on
 *  model-supply manager. It is deliberately provider-scoped: the global,
 *  read-only Creator/Model registry is the 模型 (Models) tab
 *  (settings/model_catalog_tab.js + frontend/src/features/model-catalog/),
 *  which the matrix never renders. */
var _stgMatrixQuery = {};
var _stgMatrixFilter = {};

/** Per-provider probe snapshot, keyed by provider_id. Shape:
 *  ``{ status: 'running'|'done'|'error', cells: { "<credIdx>::<wireId>":
 *      {key_idx, model_id, root_model_id, status, detail,
 *      recommend_disable} }, summary: {ok, disable}, total, done_count,
 *      error }``. */
var _stgMatrixProbe = {};

/** Active poll-timer handles, keyed by provider_id. */
var _stgMatrixProbeTimers = {};

/** Providers we've already tried to re-attach to a persisted probe this
 *  session (so re-renders don't re-fetch on every keystroke). */
var _stgMatrixProbeAttached = {};

/** Per-provider "attempts per cell" setting (filters false 429s). Default 3. */
var _stgMatrixAttempts = {};

/** The scope of the currently-running probe, keyed by provider_id.
 *  Shape: ``{key_idxs?: [int], model_ids?: [string]}`` — null/absent means a
 *  full-grid probe. Drives the per-scope spinner on the row/column/cell
 *  probe buttons. Cleared when the probe reaches a terminal state. */
var _stgMatrixProbeScope = {};

/** Shared lightning-bolt glyph for every probe trigger (toolbar + scopes). */
var _MX_BOLT = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M4 14a1 1 0 0 1-.78-1.63l9.9-10.2a.5.5 0 0 1 .86.46l-1.92 6.02A1 1 0 0 0 13 10h7a1 1 0 0 1 .78 1.63l-9.9 10.2a.5.5 0 0 1-.86-.46l1.92-6.02A1 1 0 0 0 11 14z"/></svg>';


/** Trash glyph for the remove-offering row action. */
var _MX_TRASH = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><polyline points="3 6 5 6 21 6"></polyline><path d="M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6m3 0V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2"></path><line x1="10" y1="11" x2="10" y2="17"></line><line x1="14" y1="11" x2="14" y2="17"></line></svg>';

/** Update the attempts setting for a provider from the toolbar selector. */
function _setMatrixAttempts(providerId, val) {
  _stgMatrixAttempts[providerId] = Math.max(1, Math.min(5, parseInt(val, 10) || 3));
}

/** Compose the probe-cell map key. */
function _probeCellKey(keyIdx, modelId) { return keyIdx + '::' + modelId; }

/** True while the running probe's scope IS exactly this row / column / cell
 *  (used to paint the spinner on the trigger the user clicked). */
function _scopeCovers(providerId, kind, keyIdx, modelId) {
  var s = _stgMatrixProbeScope[providerId];
  var probe = _stgMatrixProbe[providerId];
  if (!s || !probe || probe.status !== 'running') return false;
  var ks = s.key_idxs, ms = s.model_ids;
  if (kind === 'cell') {
    return !!(ks && ms && ks.length === 1 && ks[0] === keyIdx &&
              ms.length === 1 && ms[0] === modelId);
  }
  if (kind === 'col') return !!(ks && !ms && ks.length === 1 && ks[0] === keyIdx);
  if (kind === 'row') return !!(ms && !ks && ms.length === 1 && ms[0] === modelId);
  return false;
}

/** Start a row / column / single-cell probe (merged into the saved snapshot
 *  server-side; the rest of the grid keeps its verdicts). The scope arrives
 *  as scalars — the action registry has no object-literal syntax, so the
 *  ``only`` object is assembled here. */
function _probeMatrixScope(providerId, kind, first, second) {
  var probe = _stgMatrixProbe[providerId];
  if (probe && probe.status === 'running') return; // one probe per provider at a time
  var only = {};
  if (kind === 'col') only.key_idxs = [first];
  else if (kind === 'row') only.model_ids = [first];
  else if (kind === 'cell') { only.key_idxs = [first]; only.model_ids = [second]; }
  else return;
  _runMatrixProbe(providerId, false, only);
}

/** Memo of the last fit: the inputs the verdict was computed from, plus the
 *  verdict itself. Keyed on things our own width change can NOT alter:
 *   - the scroll ELEMENT references. Matrix content only ever changes through
 *     a full `_renderProvidersTab` rebuild, which returns a brand-new element
 *     — so the same element reference across two fits means the content (and
 *     its intrinsic width) is byte-identical. This is the ONLY truthful
 *     content signal: scrollWidth saturates to the panel width once wide, so
 *     no width reading can see a content change from inside the wide state.
 *   - the viewport width, which a real window resize changes.
 *   - the class state we last produced, so an external toggle re-fits.
 *  Never keyed on scrollWidth — the class we toggle feeds back into it. */
var _mxFitMemo = null;

/** Set while _fitMatrixPanelWidth mutates the panel, so the `resize` event our
 *  own width change provokes (the overlay's scrollbar appearing or
 *  disappearing) is not treated as user intent and bounced straight back. */
var _mxFitApplying = false;
var _mxFitApplyT = null;

/** The current matrix scroll elements as a plain array (NodeList in the
 *  browser, array in the node harness). */
function _mxFitScrolls() {
  var list = document.querySelectorAll('.stg-matrix-scroll');
  var out = [];
  for (var i = 0; i < list.length; i++) out.push(list[i]);
  return out;
}

/** True when nothing the verdict depends on has changed since the last fit. */
function _mxFitUnchanged(els, vw, wasWide) {
  var m = _mxFitMemo;
  if (!m || m.vw !== vw || m.wide !== wasWide || m.els.length !== els.length) return false;
  for (var i = 0; i < els.length; i++) {
    if (m.els[i] !== els[i]) return false;
  }
  return true;
}

/** Widen the settings panel when an open matrix overflows it, so 3+
 *  credential columns don't force horizontal scrolling on wide-enough
 *  screens. The class is removed as soon as no matrix overflows. */
function _fitMatrixPanelWidth() {
  var panel = document.querySelector('.modal.settings-panel');
  if (!panel) return;
  var wasWide = panel.classList.contains('stg-matrix-wide');

  // Idempotence gate. A re-fit whose inputs are unchanged must cost ZERO DOM
  // writes — no class toggle, no forced reflow, no transition edit. Every
  // periodic caller (probe poll, tab switch, the resize our own width change
  // echoes back) therefore becomes a no-op once the layout has settled.
  var scrolls = _mxFitScrolls();
  var vw = (typeof window !== 'undefined' && window.innerWidth) || 0;
  if (_mxFitUnchanged(scrolls, vw, wasWide)) return;

  _mxFitApplying = true;
  // The overflow verdict MUST be measured at the panel's DEFAULT width, never
  // at the width the class itself produces: a re-fit while the panel is wide
  // would otherwise read "no overflow" at the widened width and shrink the
  // panel right back — the expand→narrow flicker. transition:none makes the
  // class removal take effect at the forced reflow below, and everything
  // runs in one synchronous task, so no intermediate state ever paints.
  panel.style.transition = 'none';
  panel.classList.remove('stg-matrix-wide');
  var wide = false;
  for (var i = 0; i < scrolls.length; i++) {
    // Hidden matrices (inactive settings tab / collapsed provider card) have
    // a zero layout box — they must not widen the panel for something the
    // user can't see.
    if (scrolls[i].clientWidth === 0) continue;
    if (scrolls[i].scrollWidth > scrolls[i].clientWidth + 4) { wide = true; break; }
  }
  if (wide && !wasWide) {
    // Narrow→wide edge: restore the transition BEFORE the class change so the
    // single widen still animates.
    panel.style.transition = '';
    panel.classList.toggle('stg-matrix-wide', true);
  } else {
    panel.classList.toggle('stg-matrix-wide', wide);
    // Commit the final width WHILE the transition is still suspended. The
    // measurement reflow above committed the panel at its DEFAULT width, so
    // that is the value the transition engine would animate FROM: clearing
    // the transition before this commit makes every re-fit of an
    // already-wide panel animate default→wide. The 1.5s probe poll re-fits
    // forever, which turned that into a continuous narrow↔wide sweep.
    void panel.offsetWidth;
    panel.style.transition = '';
  }
  _mxFitMemo = { els: scrolls, vw: vw, wide: wide };
  // The flag must OUTLIVE this function. A scrollbar toggle caused by the
  // width change is delivered as an async `resize` on a later task, so
  // clearing synchronously here would leave the guard permanently false by
  // the time the echo lands. Hold it past the resize handler's own debounce.
  if (typeof setTimeout === 'function') {
    if (_mxFitApplyT) clearTimeout(_mxFitApplyT);
    _mxFitApplyT = setTimeout(function() { _mxFitApplying = false; }, 250);
  } else {
    _mxFitApplying = false;
  }
}

// Re-fit on window resize (debounced) — a wider viewport may make the wide
// panel unnecessary; a narrower one may need it even for 2 columns. Guarded
// for node harnesses that eval this file without DOM event APIs.
(function() {
  if (typeof window === 'undefined' || typeof window.addEventListener !== 'function') return;
  var _mxResizeT = null;
  window.addEventListener('resize', function() {
    // Our own widen/narrow reflows the overlay and can toggle the modal's
    // vertical scrollbar, which fires `resize`. Bouncing that back into a
    // re-fit is a closed loop with no user input in it, so drop the echo.
    if (_mxFitApplying) return;
    if (_mxResizeT) clearTimeout(_mxResizeT);
    _mxResizeT = setTimeout(function() {
      if (document.querySelector('.modal.settings-panel .stg-matrix-scroll')) _fitMatrixPanelWidth();
    }, 180);
  });
})();

// ── v2 data derivation ──────────────────────────────────────────────────

/** Update the matrix search text for a provider (input action). */
function _setMatrixQuery(providerId, value) {
  _stgMatrixQuery[providerId] = String(value || '');
  _rerenderMatrix(providerId);
}

/** Switch the exclusive status bucket the matrix lists. */
function _setMatrixFilter(providerId, value) {
  _stgMatrixFilter[providerId] = ['all', 'enabled', 'disabled', 'pending'].indexOf(value) >= 0
    ? value : 'all';
  _rerenderMatrix(providerId);
}

/** The credential columns of the matrix: the access's credentials in
 *  document order — the SAME order the backend's probe plan uses, so cell
 *  key ``<idx>::<wire id>`` aligns between render and probe. */
function _matrixCredentials(context) {
  return context ? context.credentials : [];
}

/** One matrix row-group per offering (confirmed AND pending — the matrix
 *  doubles as the supply manager):
 *  ``{ offering, offeringIndex, canonical, pending, retired, releaseDate,
 *      pricing, catalogPrice, wireIds, deploymentByWire, capabilities }``.
 *  ``wireIds`` contains every request name in document order, including
 *  legacy enabled:false inputs. All inherit the Offering and ModelRef grants.
 *  ``deploymentByWire`` maps ids to GLOBAL deployment indices for deletion.
 *  An offering without deployments has no request names. */
function _matrixModelRows(context) {
  var out = [];
  if (!context) return out;
  context.offerings.forEach(function(item) {
    var offering = item.row;
    var pending = offering.identity_state === 'pending_identity' || !offering.model;
    var canonical = pending
      ? String(offering.pending_model_id || offering.offering_id || '').trim()
      : String(offering.model.model_id || '').trim();
    if (!canonical) return;
    var wireIds = [], deploymentByWire = {};
    context.deployments.forEach(function(dep) {
      if (dep.row.offering_id !== offering.offering_id) return;
      var wire = String(dep.row.wire_model_id || '').trim();
      if (!wire) return;
      if (wireIds.indexOf(wire) < 0) {
        wireIds.push(wire);
        deploymentByWire[wire] = dep.index;
      }
    });
    var modelKey = pending
      ? '' : (String(offering.model.creator_id || '') + '::' + canonical);
    out.push({
      offering: offering,
      offeringIndex: item.index,
      canonical: canonical,
      pending: pending,
      retired: !pending && context.modelLifecycles[modelKey] === 'retired',
      releaseDate: pending ? '' : (context.modelReleaseDates[modelKey] || ''),
      pricing: pending ? null
        : (offering.actual_pricing || context.modelListPricing[modelKey] || null),
      catalogPrice: !pending && !offering.actual_pricing &&
        !!context.modelListPricing[modelKey],
      wireIds: wireIds,
      deploymentByWire: deploymentByWire,
      capabilities: (offering.capabilities || []).slice(),
    });
  });
  return out;
}

/** Exclusive-bucket status filter + substring match over every id the
 *  group displays (canonical + all request names). */
function _matrixGroupMatches(entry, query, filter) {
  if (filter === 'enabled' && (entry.pending || entry.offering.enabled === false)) return false;
  if (filter === 'disabled' && (entry.pending || entry.offering.enabled !== false)) return false;
  if (filter === 'pending' && !entry.pending) return false;
  if (!query) return true;
  var hay = [entry.canonical].concat(entry.wireIds)
    .join(' ').toLowerCase();
  return hay.indexOf(query) >= 0;
}

/** The wire-id pool the matrix renders/probes for one row-group. */
function _matrixRowPool(entry) {
  return entry.wireIds;
}

/** Model identity always has its own header; even an identical wire name is
 * an independently editable/removable deployment beneath it. */
function _matrixIsPureLogical(entry) {
  return true;
}

/** The model grant (ModelRef + optional wire exclusions) this credential
 *  holds for an entry, or null. */
function _matrixGrant(credentialRow, entry) {
  if (!entry.offering || !entry.offering.model) return null; // pending: no ModelRef exists
  var grants = (credentialRow.authorization && credentialRow.authorization.models) || [];
  var creator = String(entry.offering.model.creator_id || '');
  var modelId = String(entry.offering.model.model_id || '');
  for (var i = 0; i < grants.length; i++) {
    if (String(grants[i].creator_id || '') === creator &&
        String(grants[i].model_id || '') === modelId) return grants[i];
  }
  return null;
}

/** Cell state for one (credential × wire id): the model grant must exist
 *  AND this wire id must not sit in its exclusion list. When ``id`` is
 *  omitted (header aggregation), the grant alone decides. */
function _matrixCellOn(credentialRow, entry, id) {
  var grant = _matrixGrant(credentialRow, entry);
  if (!grant) return false;
  if (id === undefined || id === null) return true;
  var excluded = grant.excluded_wire_model_ids || [];
  return excluded.indexOf(String(id)) < 0;
}

/** Exclusive-bucket filter chip with a live count. */
function _mxFilterChip(providerId, active, value, label, count) {
  var on = active === value;
  return '<button type="button" class="stg-v2-filter-chip' + (on ? ' active' : '') +
    '" aria-pressed="' + (on ? 'true' : 'false') + '" data-provider-id="' + escapeHtml(providerId) +
    '" data-tofu-action="_setMatrixFilter(this.dataset.providerId,\'' + value + '\')">' +
    escapeHtml(label) + ' <em>' + count + '</em></button>';
}

/** 待确认 / 已下线 state chip for a root row (empty string when neither). */
function _mxStateChips(entry) {
  if (entry.pending) {
    return '<span class="stg-mx-chip pending" title="' + escapeHtml(t('settings.matrixPendingHint')) + '">' +
      escapeHtml(t('settings.matrixPendingChip')) + '</span>';
  }
  if (entry.retired) {
    return '<span class="stg-mx-chip retired" title="' + escapeHtml(t('settings.matrixRetiredHint')) + '">' +
      escapeHtml(t('settings.matrixRetiredChip')) + '</span>';
  }
  return '';
}

/** Compact meta line under a root row's id: capability chips + context
 *  window + release date + pricing (Model prices are labeled as reference).
 *  Pending rows render nothing — there is no catalog identity to describe. */
function _mxMetaHtml(entry) {
  if (entry.pending) return '';
  var capLabels = (typeof _MODEL_CAP_LABELS === 'object' && _MODEL_CAP_LABELS) || {};
  var caps = entry.capabilities.map(function(cap) {
    return '<span class="stg-cap ' + escapeHtml(cap) + '" title="' + escapeHtml(cap) + '">' +
      escapeHtml(capLabels[cap] || cap) + '</span>';
  }).join('');
  var bits = '';
  if (entry.offering.context_window) {
    var ctxLabel = (typeof _modelRoutingContextLabel === 'function')
      ? _modelRoutingContextLabel(entry.offering.context_window)
      : String(entry.offering.context_window);
    var contextTitle = (entry.offering.inherited_model_fields || []).indexOf('context_window') >= 0
      ? t('settings.matrixContextInheritedHint') : '';
    bits += '<span class="stg-mx-meta-bit" title="' + escapeHtml(contextTitle) + '">' + escapeHtml(t('settings.matrixContextLabel')) + ' ' +
      escapeHtml(ctxLabel) + '</span>';
  }
  if (entry.releaseDate) {
    bits += '<span class="stg-mx-meta-bit">' + escapeHtml(t('settings.matrixReleaseLabel')) + ' ' +
      escapeHtml(entry.releaseDate) + '</span>';
  }
  if (entry.pricing) {
    var priceTitle = entry.catalogPrice ? t('settings.matrixReferencePriceHint') : t('settings.matrixPriceTitle');
    bits += '<span class="stg-mx-meta-bit stg-mx-price" title="' + escapeHtml(priceTitle) + '">' +
      '<span class="stg-v2-price-cur">' + escapeHtml(entry.pricing.currency || 'USD') + '</span>' +
      '<span class="stg-v2-price-io">' + escapeHtml(t('settings.matrixPriceIn')) + '</span>' +
      escapeHtml(String(Number(entry.pricing.input || 0))) +
      '<span class="stg-v2-price-sep">/</span>' +
      '<span class="stg-v2-price-io">' + escapeHtml(t('settings.matrixPriceOut')) + '</span>' +
      escapeHtml(String(Number(entry.pricing.output || 0))) +
      (entry.catalogPrice
        ? '<span class="stg-v2-price-tag">' + escapeHtml(t('settings.matrixCatalogPrice')) + '</span>'
        : '') +
    '</span>';
  }
  if (!caps && !bits) return '';
  return '<span class="stg-mx-meta">' + caps + bits + '</span>';
}

// ── Render ──────────────────────────────────────────────────────────────

/** Build the full access-matrix table for a provider — the provider's
 *  always-on model-supply manager: search + status buckets + add-model on
 *  the manage row, probe tools on the second toolbar row. */
function _renderAccessMatrix(providerId) {
  var context = _modelRoutingProviderContext(providerId);
  if (!context) return '';
  var credentials = _matrixCredentials(context);
  var allRows = _matrixModelRows(context);

  // Lazily re-attach to a persisted/running probe the first time we render
  // this provider's matrix in a session.
  if (!_stgMatrixProbeAttached[providerId]) {
    _stgMatrixProbeAttached[providerId] = true;
    setTimeout(function() { _resumeMatrixProbe(providerId); }, 0);
  }

  // Exclusive status buckets + substring search over every displayed id.
  var counts = { all: allRows.length, enabled: 0, disabled: 0, pending: 0 };
  allRows.forEach(function(entry) {
    if (entry.pending) { counts.pending++; return; }
    if (entry.offering.enabled === false) counts.disabled++;
    else counts.enabled++;
  });
  var query = String(_stgMatrixQuery[providerId] || '');
  var filter = _stgMatrixFilter[providerId] || 'all';
  var q = query.trim().toLowerCase();
  var rows = allRows.filter(function(entry) {
    return _matrixGroupMatches(entry, q, filter);
  }).sort(function(left, right) {
    // Disabled offerings sink below usable ones (pending counts as usable).
    var rank = ((right.offering.enabled === false) ? 0 : 1) -
               ((left.offering.enabled === false) ? 0 : 1);
    if (rank) return rank;
    return left.canonical.localeCompare(right.canonical, undefined,
      { numeric: true, sensitivity: 'base' });
  });

  var probe = _stgMatrixProbe[providerId] || {};
  var running = (probe.status === 'running');
  var hasResults = probe.cells && Object.keys(probe.cells).length > 0;
  var recommendCount = (probe.summary && probe.summary.disable) || 0;
  // The button counts every grant Apply would change — disables AND
  // enablements — derived from the same projection Apply executes, so an
  // enable-only recommendation surfaces the action instead of a dead end.
  var applyCount = _matrixApplyCount(providerId);

  var statusTxt = '';
  if (running) {
    statusTxt = t('settings.matrixProbing') +
      (probe.total ? ' (' + (probe.done_count || 0) + '/' + probe.total + ')' : '');
  } else if (probe.status === 'error') {
    var probeError = (typeof errorEnvelopeMessage === 'function')
      ? errorEnvelopeMessage(probe.error) : String(probe.error || '');
    statusTxt = probe.start_error ? probeError
      : t('settings.matrixProbeFailed') + (probeError ? ': ' + probeError : '');
  } else if (hasResults) {
    statusTxt = (probe.summary.ok || 0) + ' ' + t('settings.matrixOkCount') +
      ' · ' + recommendCount + ' ' + t('settings.matrixFlaggedCount') +
      ((probe.summary.skipped || 0) > 0
        ? ' · ' + probe.summary.skipped + ' ' + t('settings.matrixSkippedCount')
        : '');
  }

  var html = '<div class="stg-matrix" data-provider-id="' + escapeHtml(providerId) + '">' +
    '<div class="stg-mx-manage">' +
      (allRows.length
        ? '<input id="stgMatrixSearch-' + escapeHtml(providerId) + '" class="stg-mx-search" type="search" ' +
          'placeholder="' + escapeHtml(t('settings.matrixSearchPlaceholder')) + '" ' +
          'value="' + escapeHtml(query) + '" data-provider-id="' + escapeHtml(providerId) + '" ' +
          'data-tofu-action-input="_setMatrixQuery(this.dataset.providerId,this.value)">' +
          '<div class="stg-mx-filters" role="group" aria-label="' + escapeHtml(t('settings.matrixFilterAria')) + '">' +
            _mxFilterChip(providerId, filter, 'all', t('settings.matrixFilterAll'), counts.all) +
            _mxFilterChip(providerId, filter, 'enabled', t('settings.matrixFilterEnabled'), counts.enabled) +
            _mxFilterChip(providerId, filter, 'disabled', t('settings.matrixFilterDisabled'), counts.disabled) +
            _mxFilterChip(providerId, filter, 'pending', t('settings.matrixFilterPending'), counts.pending) +
          '</div>'
        : '') +
      '<button type="button" class="stg-btn-add stg-mx-addmodel" data-provider-id="' + escapeHtml(providerId) + '" ' +
        'data-tofu-action="_openProviderModelPicker(this.dataset.providerId)">+ ' +
        escapeHtml(t('settings.matrixAddModel')) + '</button>' +
    '</div>';

  if (!allRows.length) {
    return html + '<p class="stg-mx-empty">' + escapeHtml(t('settings.matrixEmptyModels')) + '</p></div>';
  }

  if (credentials.length) {
    html += '<div class="stg-matrix-toolbar">' +
      '<div class="stg-matrix-legend">' +
        '<span class="stg-mx-leg on"><span class="stg-mx-dot"></span>' + escapeHtml(t('settings.matrixLegendOn')) + '</span>' +
        '<span class="stg-mx-leg off"><span class="stg-mx-dot"></span>' + escapeHtml(t('settings.matrixLegendOff')) + '</span>' +
      '</div>' +
      '<div class="stg-matrix-tools">' +
        (hasResults && applyCount > 0 && !running
          ? '<button type="button" class="stg-btn-add stg-mx-apply" data-provider-id="' + escapeHtml(providerId) + '" data-tofu-action="_applyMatrixRecommendations(this.dataset.providerId)" title="' + escapeHtml(t('settings.matrixApplyHint')) + '">✓ ' + escapeHtml(t('settings.matrixApplyRec')) + ' (' + applyCount + ')</button>'
          : '') +
        (hasResults && !running ? '<button type="button" class="stg-btn-add" data-provider-id="' + escapeHtml(providerId) + '" data-tofu-action="_clearMatrixProbe(this.dataset.providerId)" title="' + escapeHtml(t('settings.matrixClearProbe')) + '">' + escapeHtml(t('settings.matrixClearProbe')) + '</button>' : '') +
        (running ? '' :
          '<label class="stg-mx-attempts" title="' + escapeHtml(t('settings.matrixAttemptsHint')) + '">' + escapeHtml(t('settings.matrixAttempts')) +
            '<select data-tofu-dd data-provider-id="' + escapeHtml(providerId) + '" data-tofu-action-change="_setMatrixAttempts(this.dataset.providerId,this.value)">' +
              [1, 2, 3, 4, 5].map(function(n) {
                var sel = (n === (_stgMatrixAttempts[providerId] || 3)) ? ' selected' : '';
                return '<option value="' + n + '"' + sel + '>×' + n + '</option>';
              }).join('') +
            '</select></label>') +
        '<button type="button" class="stg-btn-add stg-mx-probe' + (running ? ' running' : '') + '"' + (running ? ' disabled' : '') +
          ' data-provider-id="' + escapeHtml(providerId) + '"' +
          ' data-tofu-action="_runMatrixProbe(this.dataset.providerId,' + (hasResults ? 'true' : 'false') + ')" title="' + escapeHtml(t('settings.matrixProbeHint')) + '"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" style="width:1em;height:1em;vertical-align:-2px"><path d="M4 14a1 1 0 0 1-.78-1.63l9.9-10.2a.5.5 0 0 1 .86.46l-1.92 6.02A1 1 0 0 0 13 10h7a1 1 0 0 1 .78 1.63l-9.9 10.2a.5.5 0 0 1-.86-.46l1.92-6.02A1 1 0 0 0 11 14z"/></svg> ' +
          escapeHtml(running ? t('settings.matrixProbing') : (hasResults ? t('settings.matrixRetest') : t('settings.matrixProbe'))) + '</button>' +
      '</div>' +
    '</div>' +
    (statusTxt ? '<div class="stg-mx-status' + (running ? ' running' : (probe.status === 'error' ? ' error' : '')) + '">' + escapeHtml(statusTxt) + '</div>' : '');
  } else {
    html += '<p class="stg-mx-nokeys">' + escapeHtml(t('settings.matrixNoKeysHint')) + '</p>';
  }

  html += '<div class="stg-matrix-scroll"><table class="stg-matrix-table"><thead><tr>' +
    '<th class="stg-mx-corner">' + escapeHtml(t(credentials.length ? 'settings.matrixModelCol' : 'settings.matrixModelColSolo')) + '</th>';

  for (var ci = 0; ci < credentials.length; ci++) {
    var cred = credentials[ci].row;
    var credKind = String(cred.kind || 'api_key');
    // The masked recognition hint (sk-…•••…wxyz) is the only way to tell two
    // same-kind credentials apart; fall back to the bare kind for hint-less
    // rows (local identity, legacy oauth snapshots).
    var credHint = String(cred.key_hint || '').trim();
    html += '<th class="stg-mx-keyhead" data-key-idx="' + ci + '">' +
      '<span class="stg-mx-credname">' + escapeHtml(t('settings.matrixCredentialN').replace('{n}', String(ci + 1))) + '</span>' +
      '<span class="stg-mx-credkind" title="' + escapeHtml(credHint ? credKind + ' · ' + credHint : credKind) + '">' +
        escapeHtml(credHint || credKind) + '</span>' +
      '<button type="button" class="stg-mx-zap col' + (_scopeCovers(providerId, 'col', ci) ? ' probing' : '') + '"' +
        (running ? ' disabled' : '') +
        ' data-provider-id="' + escapeHtml(providerId) + '"' +
        ' data-tofu-action="_probeMatrixScope(this.dataset.providerId,\'col\',' + ci + ')" ' +
        'title="' + escapeHtml(t('settings.matrixProbeColHint')) + '">' + _MX_BOLT + '</button>' +
    '</th>';
  }
  html += '</tr></thead><tbody>';

  if (!rows.length) {
    html += '<tr><td class="stg-mx-nomatch" colspan="' + (credentials.length + 1) + '">' +
      escapeHtml(t('settings.matrixNoMatch')) + '</td></tr>';
  }

  for (var ri = 0; ri < rows.length; ri++) {
    var entry = rows[ri];
    var pool = _matrixRowPool(entry);
    var displayCount = pool.length;
    var groupOpen = displayCount > 0; // only bracket offerings that HAVE a pool
    if (_matrixIsPureLogical(entry)) {
      html += _renderMatrixRow(providerId, entry, entry.canonical, -1, displayCount, credentials, groupOpen);
      for (var li = 0; li < pool.length; li++) {
        html += _renderMatrixRow(providerId, entry, pool[li], li + 1, displayCount, credentials, groupOpen);
      }
    } else {
      for (var wi = 0; wi < pool.length; wi++) {
        html += _renderMatrixRow(providerId, entry, pool[wi], wi, displayCount, credentials, groupOpen);
      }
    }
  }
  html += '</tbody></table></div></div>';
  return html;
}

/** Render one matrix row. Kinds:
 *   - LOGICAL HEADER (``rowPos === -1``): the preset-facing canonical id of
 *     an offering whose wire pool never carries it. It carries the offering
 *     toggle and the wire-id count, but NO per-credential cells — the id is
 *     never sent on the wire, so there is no (credential × id) pair to
 *     grant, deny, or probe.
 *   - WIRE ROW (``rowPos >= 0``): one concrete upstream id across
 *     credentials. ``rowPos`` is the 1-based index under a logical header,
 *     or 0 = root for a legacy-shape offering (canonical id IS a wire id).
 *  Root rows double as the supply manager: state chips (待确认/已下线),
 *  capability/pricing/context meta line, and row actions (row probe, add
 *  request name, remove offering). */
function _renderMatrixRow(providerId, entry, id, rowPos, rowCount, credentials, grouped) {
  var isLogicalHead = (rowPos === -1);
  var isAlias = rowPos > 0;
  var underHead = _matrixIsPureLogical(entry);
  var isLastInGroup = underHead ? (rowPos === rowCount) : (rowPos === rowCount - 1);
  var globallyOff = (entry.offering.enabled === false);
  // Logical headers have no wire request; provider-scoped rows remain probeable.
  var hatched = isLogicalHead;
  var brand = (typeof _modelBrand === 'function')
    ? _modelBrand(id, entry.offering && entry.offering.model && entry.offering.model.creator_id)
    : ((typeof _detectBrand === 'function') ? _detectBrand(id) : '');
  var brandSvg = (typeof _brandSvg === 'function') ? _brandSvg(brand, 14) : '';

  // Row-scope probe button: probes exactly this wire id across every credential.
  var _rowProbe = _stgMatrixProbe[providerId] || {};
  var _rowRunning = (_rowProbe.status === 'running');
  var rowProbeBtn = (hatched || !credentials.length) ? '' : '<button type="button" class="stg-mx-zap row' +
      (_scopeCovers(providerId, 'row', null, id) ? ' probing' : '') + '"' +
    (_rowRunning ? ' disabled' : '') +
    ' data-provider-id="' + escapeHtml(providerId) + '"' +
    ' data-tofu-action="event.stopPropagation();_probeMatrixScope(this.dataset.providerId' +
      ',\'row\',' + JSON.stringify(id).replace(/"/g, '&quot;') + ')" ' +
    'title="' + escapeHtml(t('settings.matrixProbeRowHint')) + '">' + _MX_BOLT + '</button>';

  var labelCell;
  if (isAlias) {
    var connector = isLastInGroup ? '└' : '├';
    // A distinct accent color per wire-id index, cycled, so two ids of the
    // same offering never look alike at a glance.
    var hue = (entry.offeringIndex * 47 + rowPos * 71) % 360;
    var depIndex = entry.deploymentByWire[id];
    labelCell = '<td class="stg-mx-model alias' + (globallyOff ? ' model-off' : '') +
        (isLastInGroup ? ' last' : '') +
        '" style="--alias-hue:' + hue + '">' +
      '<span class="stg-mx-tree">' + connector + '</span>' +
      '<span class="stg-mx-aliasidx">' + rowPos + '</span>' +
      '<span class="stg-mx-brand">' + brandSvg + '</span>' +
      '<span class="stg-mx-mid alias-id" data-request-deployment="' +
        escapeHtml((_stgModelRouting.deployments[depIndex] || {}).deployment_id || '') + '" title="' + escapeHtml(id) + '">' + escapeHtml(id) + '</span>' +
      '<span class="stg-mx-rowacts">' + rowProbeBtn +
        (depIndex != null
          ? '<button type="button" class="stg-mx-act del" title="' + escapeHtml(t('settings.matrixRemoveAliasHint')) + '" ' +
            'data-tofu-action="event.stopPropagation();_removeV2Alias(' + depIndex + ')">×</button>'
          : '') +
      '</span>' +
    '</td>';
  } else {
    var poolLen = entry.wireIds.length;
    var countBadge = (!entry.pending)
      ? '<span class="stg-mx-aliascount" title="' + escapeHtml(t('settings.matrixAliasCountHint')) + '">' +
          poolLen + ' ' + escapeHtml(t('settings.matrixIdMany')) + '</span>'
      : '';
    var presetBadge = isLogicalHead
      ? '<span class="stg-mx-preset" title="' + escapeHtml(t('settings.matrixPresetHint')) + '">' +
          escapeHtml(t('settings.matrixPresetBadge')) + '</span>'
      : '';
    var rootActs = '<span class="stg-mx-rowacts">' + rowProbeBtn +
      '<button type="button" class="stg-mx-act add" title="' + escapeHtml(t('settings.matrixAddAliasHint')) + '" ' +
        'data-provider-id="' + escapeHtml(providerId) + '" ' +
        'data-tofu-action="event.stopPropagation();_addV2Alias(this.dataset.providerId,\'' +
          escapeHtml(entry.offering.offering_id) + '\')">+</button>' +
      '<button type="button" class="stg-mx-act del danger" title="' + escapeHtml(t('settings.matrixRemoveOfferingHint')) + '" ' +
        'data-provider-id="' + escapeHtml(providerId) + '" ' +
        'data-tofu-action="event.stopPropagation();_removeV2Offering(this.dataset.providerId,\'' +
          escapeHtml(entry.offering.offering_id) + '\')">' + _MX_TRASH + '</button>' +
      '</span>';
    labelCell = '<td class="stg-mx-model root' + (isLogicalHead ? ' logical' : '') +
        (globallyOff ? ' model-off' : '') + (entry.pending ? ' pending' : '') +
        (entry.retired ? ' retired' : '') + '">' +
      '<label class="stg-toggle stg-mx-gtoggle" title="' + escapeHtml(t('settings.matrixGlobalToggle')) + '" data-tofu-action="event.stopPropagation();">' +
        '<input type="checkbox"' + (globallyOff ? '' : ' checked') +
          ' data-tofu-action-change="_setModelRoutingCollectionField(\'offerings\',' +
          entry.offeringIndex + ',\'enabled\',this.checked,\'boolean\')">' +
        '<span class="stg-toggle-track"><span class="stg-toggle-thumb"></span></span>' +
      '</label>' +
      '<span class="stg-mx-brand">' + brandSvg + '</span>' +
      '<span class="stg-mx-mid' + (entry.retired ? ' retired' : '') + '" title="' + escapeHtml(id || '') + '">' +
        escapeHtml(id || '(unnamed)') + '</span>' +
      _mxStateChips(entry) +
      presetBadge +
      countBadge +
      rootActs +
      _mxMetaHtml(entry) +
    '</td>';
  }

  var cls = 'stg-mx-row' + (globallyOff ? ' model-off' : '') +
    (isAlias ? ' is-alias' : ' is-root') + (isLogicalHead ? ' is-logical' : '') +
    (grouped ? ' grouped' : '') + (isLastInGroup && grouped ? ' group-end' : '') +
    (entry.pending ? ' is-pending' : '') +
    (entry.retired ? ' is-retired' : '');
  var row = '<tr class="' + cls + '" data-offering="' + entry.offeringIndex + '" data-id="' + escapeHtml(id) + '">' + labelCell;
  if (hatched) {
    var hatchTitle = isLogicalHead ? '' :
      ' title="' + escapeHtml(t('settings.matrixPendingHint')) + '"';
    for (var hk = 0; hk < credentials.length; hk++) {
      row += '<td class="stg-mx-cell logical"' + hatchTitle + '></td>';
    }
  } else {
    for (var k = 0; k < credentials.length; k++) {
      row += _renderMatrixCell(providerId, entry, k, credentials[k].row, id);
    }
  }
  row += '</tr>';
  return row;
}

/** Probe status → {glyph, cls, label} for the cell health pip. */
function _probeStatusInfo(status) {
  switch (status) {
    case 'ok':           return { glyph: '✓', cls: 'ok',     label: t('settings.probeOk') };
    case 'bad_request':  return { glyph: '400', cls: 'err',  label: t('settings.probeBadRequest') };
    case 'invalid_response': return { glyph: '∅', cls: 'err', label: t('settings.probeInvalidResponse') };
    case 'rate_limited': return { glyph: '429', cls: 'rate', label: t('settings.probeRateLimited') };
    case 'unauthorized': return { glyph: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" style="width:1em;height:1em;vertical-align:-2px"><circle cx="12" cy="12" r="10"/><path d="M4.929 4.929 19.07 19.071"/></svg>', cls: 'unauth', label: t('settings.probeUnauthorized') };
    case 'not_found':    return { glyph: '∅', cls: 'nf',     label: t('settings.probeNotFound') };
    case 'unavailable':  return { glyph: '⚠', cls: 'down',   label: t('settings.probeUnavailable') };
    case 'skipped':      return { glyph: 'N/A', cls: 'skip', label: t('settings.probeSkipped') };
    case 'unverified':   return { glyph: '?', cls: 'skip',   label: t('settings.probeUnverified') };
    case 'not_logged_in': return { glyph: '↪', cls: 'skip',  label: t('settings.probeNotLoggedIn') };
    default:             return { glyph: '✕', cls: 'err',    label: t('settings.probeError') };
  }
}

/** Render one matrix cell (a single (credential × wire id) access view).
 *  The dot reflects the credential's authorization grant for the row's
 *  offering (per-model, so all wire rows of an offering share it); the pip
 *  is the exact (credential, wire id) probe verdict. */
function _renderMatrixCell(providerId, entry, credIdx, credentialRow, id) {
  var on = _matrixCellOn(credentialRow, entry, id);

  // Probe-status pip: exact (credential, wire id) result.
  var probe = _stgMatrixProbe[providerId] || {};
  var pcells = probe.cells || {};
  var running = (probe.status === 'running');
  var pip = '';
  var cellProbe = '';
  var cellOnly = '\'cell\',' + credIdx + ',' +
    JSON.stringify(id).replace(/"/g, '&quot;');
  var r = pcells[_probeCellKey(credIdx, id)];
  if (_scopeCovers(providerId, 'cell', credIdx, id)) {
    // This cell is being probed right now — spin a bolt in place of the pip.
    cellProbe = '<span class="stg-mx-zap cell probing" title="' +
      escapeHtml(t('settings.matrixProbing')) + '">' + _MX_BOLT + '</span>';
  } else if (r) {
    var info = _probeStatusInfo(r.status);
    // The pip doubles as the re-probe trigger for its own cell.
    pip = '<span class="stg-mx-probe-pip ' + info.cls + ' clickable" role="button" ' +
      'title="' + escapeHtml(info.label + (r.detail ? ' — ' + r.detail : '') +
        '\n' + t('settings.matrixProbeCellHint')) + '" ' +
      'data-provider-id="' + escapeHtml(providerId) + '"' +
      'data-tofu-action="event.stopPropagation();_probeMatrixScope(this.dataset.providerId,' + cellOnly + ')">' +
      info.glyph + '</span>';
  } else {
    // Never probed — hover reveals a single-cell probe button (bottom-left).
    cellProbe = '<button type="button" class="stg-mx-zap cell"' + (running ? ' disabled' : '') +
      ' data-provider-id="' + escapeHtml(providerId) + '"' +
      ' data-tofu-action="event.stopPropagation();_probeMatrixScope(this.dataset.providerId,' + cellOnly + ')" ' +
      'title="' + escapeHtml(t('settings.matrixProbeCellHint')) + '">' + _MX_BOLT + '</button>';
  }

  return '<td class="stg-mx-cell' + (entry.pending ? ' provider-model' : (on ? ' on' : ' off')) +
      '" data-offering="' + entry.offeringIndex + '" data-key-idx="' + credIdx + '" data-id="' + escapeHtml(id) + '">' +
    (entry.pending ? '' : '<button type="button" class="stg-mx-toggle" ' +
      'data-provider-id="' + escapeHtml(providerId) + '"' +
      ' data-wire-id="' + escapeHtml(String(id)) + '"' +
      ' data-tofu-action="_toggleMatrixAccess(this.dataset.providerId,' + entry.offeringIndex + ',' + credIdx + ',this.dataset.wireId)" ' +
      'title="' + escapeHtml(on ? t('settings.matrixClickDisable') : t('settings.matrixClickEnable')) + '">' +
      '<span class="stg-mx-dot"></span>' +
    '</button>') +
    pip +
    cellProbe +
  '</td>';
}

// ── Interactions ──────────────────────────────────────────────────────

/** Toggle one (credential × wire id) cell. The grant is a model ModelRef
 *  plus an exclusion list of that model's wire ids; flipping one cell adds
 *  or removes this wire id from the exclusions. At the boundaries the model
 *  grant itself is minted (enabling when no grant exists) or dropped
 *  (disabling the last reachable wire). The change lives in
 *  ``_stgModelRouting`` and is persisted by the settings 保存 flow. */
function _toggleMatrixAccess(providerId, offeringIndex, credIdx, wireId) {
  var context = _modelRoutingProviderContext(providerId);
  if (!context) return;
  var offering = (_stgModelRouting.offerings || [])[offeringIndex];
  // credIdx is the column position inside THIS provider's matrix, not an
  // index into the global credentials array — resolve through the
  // provider-scoped context (its .row aliases the global object), or any
  // access whose credentials don't start at global index 0 toggles a
  // DIFFERENT provider's credential and its own dot never flips.
  var credItem = context.credentials[credIdx];
  var credential = credItem && credItem.row;
  if (!offering || !credential || !offering.model) return;
  if (!credential.authorization) credential.authorization = { connection_ids: [], models: [] };
  var grants = credential.authorization.models || [];
  var creator = String(offering.model.creator_id || '');
  var modelId = String(offering.model.model_id || '');
  // Live callers always pass the row's wire id; a bare call (legacy tests,
  // programmatic toggles) resolves to the row's single/primary wire id so
  // the exclusion can never be keyed on an empty string.
  var pool = _matrixRowPool(_matrixEntryFor(context, offeringIndex)).map(String);
  var wire = (wireId === undefined || wireId === null || wireId === '')
    ? (pool[0] || '') : String(wireId);
  var grant = null;
  for (var i = 0; i < grants.length; i++) {
    if (String(grants[i].creator_id || '') === creator &&
        String(grants[i].model_id || '') === modelId) { grant = grants[i]; break; }
  }
  if (!grant) {
    // No grant at all → enabling. Mint a grant with no exclusions (the key
    // stays absent — a grant without it reaches every wire of the model).
    grants.push(JSON.parse(JSON.stringify(offering.model)));
    credential.authorization.models = grants;
    _rerenderMatrix(providerId);
    return;
  }
  var excluded = (grant.excluded_wire_model_ids || []).slice();
  var pos = excluded.indexOf(wire);
  if (pos >= 0) {
    // Currently excluded → enable this wire by lifting the exclusion.
    excluded.splice(pos, 1);
    if (excluded.length) grant.excluded_wire_model_ids = excluded;
    else delete grant.excluded_wire_model_ids;
    _rerenderMatrix(providerId);
    return;
  }
  // Currently reachable → disable this wire. When it is the model's last
  // reachable wire for this credential, drop the whole grant instead of
  // leaving a grant that reaches nothing.
  var reachableNow = pool.filter(function(id) {
    return excluded.indexOf(String(id)) < 0;
  });
  if (reachableNow.length <= 1 && reachableNow.indexOf(wire) >= 0) {
    credential.authorization.models = grants.filter(function(ref) {
      return !(String(ref.creator_id || '') === creator && String(ref.model_id || '') === modelId);
    });
  } else {
    excluded.push(wire);
    grant.excluded_wire_model_ids = excluded;
  }
  _rerenderMatrix(providerId);
}

/** Row entry for one offering index inside this provider context. */
function _matrixEntryFor(context, offeringIndex) {
  var rows = _matrixModelRows(context);
  for (var i = 0; i < rows.length; i++) {
    if (rows[i].offeringIndex === offeringIndex) return rows[i];
  }
  return { wireIds: [], canonical: '' };
}

/** Re-render the providers tab; open cards and per-provider matrix state
 *  are preserved by the tab renderer + ``_stgMatrixQuery``/``_stgMatrixFilter``.
 *  The matrix search box keeps focus + caret across the rebuild so typing
 *  doesn't drop the field on every keystroke. */
function _rerenderMatrix(providerId) {
  var refocus = null;
  if (typeof document !== 'undefined' && document.activeElement) {
    var active = document.activeElement;
    if (active && active.id && String(active.id).indexOf('stgMatrixSearch-') === 0) {
      refocus = { id: active.id, start: active.selectionStart, end: active.selectionEnd };
    }
  }
  _renderProvidersTab();
  if (refocus) {
    var el = document.getElementById(refocus.id);
    if (el && typeof el.focus === 'function') {
      el.focus();
      try { el.setSelectionRange(refocus.start, refocus.end); } catch (e) { /* non-text input */ }
    }
  }
  if (typeof _fitMatrixPanelWidth === 'function') _fitMatrixPanelWidth();
}

// ── Background probe: start / poll / resume / apply ───────────────────────

/** True when the offering has no chat surface (image_gen / embedding /
 *  transcription). Reads the shared taxonomy helper when available, else
 *  the same hardcoded fallback set it ships with. */
function _matrixModelIsNonChat(entry) {
  if (!entry) return false;
  if (typeof runtimeScope.isChatModel === 'function') {
    return !runtimeScope.isChatModel({ capabilities: entry.capabilities });
  }
  var nonChat = ['image_gen', 'embedding', 'transcription', 'tts'];
  for (var i = 0; i < entry.capabilities.length; i++) {
    if (nonChat.indexOf(entry.capabilities[i]) >= 0) return true;
  }
  return false;
}

/** True when the cell carries a verdict from the model's OWN modality
 *  probe (image / transcription / embedding). Cells stamped 'chat', 'none',
 *  or carrying no stamp at all (pre-stamp snapshots) are NOT modality
 *  verdicts — for a non-chat model those are the stale kind. */
function _isFreshModalityVerdict(c) {
  return !!(c && c.probe_surface && c.probe_surface !== 'chat' &&
            c.probe_surface !== 'none');
}

/** Downgrade STALE probe cells for non-chat models to 'skipped'.
 *
 *  Snapshots persisted before the per-modality probes existed carry false
 *  'unavailable' verdicts produced by a CHAT-completions probe (the gateway
 *  deterministically 500s it for image/embedding models) with
 *  recommend_disable=true — applying them would disable WORKING image
 *  models. A cell is stale when its probe_surface is missing or 'chat';
 *  a verdict stamped with the model's OWN modality surface (e.g. an
 *  image-surface not_found) is FRESH and must reach the user untouched.
 *  Reconciliation runs on every ingest so old disk snapshots heal without
 *  forcing a retest; the original verdict is kept in the tooltip. */
function _reconcileProbeNonChat(providerId) {
  var probe = _stgMatrixProbe[providerId];
  var context = _modelRoutingProviderContext(providerId);
  if (!probe || !probe.cells || !context) return;
  var byRoot = {};
  _matrixModelRows(context).forEach(function(entry) { byRoot[entry.canonical] = entry; });
  var changed = false;
  Object.keys(probe.cells).forEach(function(k) {
    var c = probe.cells[k];
    if (!c || c.status === 'ok' || c.status === 'skipped') return;
    if (_isFreshModalityVerdict(c)) return;   // real modality verdict — keep
    var entry = byRoot[c.root_model_id];
    if (!_matrixModelIsNonChat(entry)) return;
    c.detail = 'stale chat-probe verdict discarded (non-chat model) — re-run ' +
               'the probe to test it via its real endpoint (was ' + c.status +
               (c.detail ? ': ' + c.detail : '') + ')';
    c.status = 'skipped';
    c.recommend_disable = false;
    changed = true;
  });
  if (changed) _mxRecountSummary(probe);
}

/** Enforce the strict proof contract in the browser as well as the server.
 * This is load-bearing during rolling deploys and for old persisted cache:
 * an old backend can still send ``ok + HTTP 400`` and the UI must never turn
 * that contradiction into a green pip. The legacy-status migration no-ops
 * when the typed status helper is absent (the server-side snapshot
 * normalization then owns the rule); the disabled-cell strip below is not
 * part of that migration and always runs. */
function _reconcileProbeProofContract(providerId) {
  var probe = _stgMatrixProbe[providerId];
  if (!probe || !probe.cells) return;
  var migrate = typeof runtimeScope.effectiveProbeStatus === 'function';
  var changed = false;
  Object.keys(probe.cells).forEach(function(k) {
    var c = probe.cells[k];
    if (!c) return;
    if (migrate) {
      var effective = runtimeScope.effectiveProbeStatus(c, probe.probe_schema_version || 1);
      if (effective !== c.status) {
        var previous = c.status;
        c.status = effective;
        c.recommend_disable = (effective === 'bad_request' ||
          effective === 'invalid_response' || effective === 'error');
        if (previous === 'ok' && effective === 'bad_request' &&
            String(c.detail || '').indexOf('false-positive corrected') < 0) {
          c.detail = (c.detail || 'HTTP 400') +
            ' — legacy false-positive corrected; provider rejected the request';
        } else if (previous === 'ok' && effective === 'unverified') {
          c.detail = (c.detail || 'HTTP 2xx') +
            ' — legacy result did not validate generated content; re-test required';
        }
        changed = true;
      }
    }
    // Historical discovery snapshots stay non-actionable after retiring
    // request-name enablement; fresh probes no longer emit this marker.
    if (c.deployment_enabled === false && c.recommend_disable) {
      c.recommend_disable = false;
      changed = true;
    }
  });
  if (changed) _mxRecountSummary(probe);
}

/** Recompute probe.summary over ALL current cells (mirrors the backend's
 *  ``_recount_summary``): shared by the non-chat reconcile and the
 *  stale-cell prune, which must never drift apart. */
function _mxRecountSummary(probe) {
  var ok = 0, disable = 0, skipped = 0, neutral = 0, failed = 0;
  Object.keys(probe.cells).forEach(function(k) {
    var c = probe.cells[k];
    if (!c) return;
    if (c.status === 'ok') ok++;
    else if (c.status === 'skipped') skipped++;
    else if (c.status === 'unverified' || c.status === 'not_logged_in') neutral++;
    else failed++;
    if (c.recommend_disable) disable++;
  });
  probe.summary = { ok: ok, disable: disable, skipped: skipped,
                    neutral: neutral, failed: failed };
}

/** Drop probe cells whose (credential × wire id) no longer exists in the
 *  provider's CURRENT grid. A persisted snapshot outlives the config it
 *  measured: a deleted offering/credential/deployment leaves cells with no
 *  row at all, and rendering such a ghost makes a stale 'reachable ✓' look
 *  like real coverage. Mirrors the backend's scoped-probe seed prune. */
function _pruneProbeCellsToGrid(providerId) {
  var probe = _stgMatrixProbe[providerId];
  var context = _modelRoutingProviderContext(providerId);
  if (!probe || !probe.cells || !context) return;
  var credentials = _matrixCredentials(context);
  if (!credentials.length) return; // no columns → no grid; nothing to validate against
  var valid = {};
  for (var ci = 0; ci < credentials.length; ci++) {
    _matrixModelRows(context).forEach(function(entry) {
      var pool = _matrixRowPool(entry);
      for (var ri = 0; ri < pool.length; ri++) valid[_probeCellKey(ci, pool[ri])] = true;
    });
  }
  var changed = false;
  Object.keys(probe.cells).forEach(function(k) {
    if (!valid[k]) { delete probe.cells[k]; changed = true; }
  });
  if (changed) _mxRecountSummary(probe);
}

/** Normalise a backend snapshot into the local _stgMatrixProbe entry.
 *  Returns true when the snapshot carried real probe data. */
function _ingestProbeSnapshot(providerId, snap) {
  if (!snap || snap.status === 'none') return false;
  _stgMatrixProbe[providerId] = {
    probe_schema_version: snap.probe_schema_version || 1,
    status: snap.status || 'done',
    cells: snap.cells || {},
    summary: snap.summary || { ok: 0, disable: 0 },
    total: snap.total || 0,
    done_count: snap.done_count || (snap.cells ? Object.keys(snap.cells).length : 0),
    attempts: snap.attempts || null,
    error: snap.error || null,
  };
  // Reflect the server's attempts setting in the selector on resume.
  if (snap.attempts && !_stgMatrixAttempts[providerId]) _stgMatrixAttempts[providerId] = snap.attempts;
  if (_stgMatrixProbe[providerId].status !== 'running') delete _stgMatrixProbeScope[providerId];
  _reconcileProbeProofContract(providerId);
  _pruneProbeCellsToGrid(providerId);
  _reconcileProbeNonChat(providerId);
  return true;
}

/** Start (or, when not forcing, resume) a background probe for a provider.
 *  ``only`` (optional) scopes the run to rows/columns/cells:
 *  ``{key_idxs?: [int], model_ids?: [string]}`` — the backend probes exactly
 *  those cells and MERGES the verdicts into the persisted snapshot. */
async function _runMatrixProbe(providerId, force, only) {
  var context = _modelRoutingProviderContext(providerId);
  if (!context) return;
  var existing = _stgMatrixProbe[providerId];
  if (existing && existing.status === 'running') return; // one probe per provider at a time
  if (!_matrixCredentials(context).length || !_matrixModelRows(context).length) {
    if (typeof showToast === 'function') showToast(t('settings.matrixNothingToProbe'), 'warning');
    return;
  }

  _stgMatrixProbeScope[providerId] = only || null;
  _stgMatrixProbe[providerId] = { status: 'running', cells: (force ? {} : ((_stgMatrixProbe[providerId] || {}).cells || {})),
    summary: { ok: 0, disable: 0 }, total: 0, done_count: 0, error: null };
  _rerenderMatrix(providerId);

  try {
    // Joining an existing model-add save may establish its Offering. Detect
    // never initiates a save, and a rejected add must abort instead of probing
    // an older selection. Re-read the draft after the chain settles.
    if (_stgRoutingPersistChain) await _stgRoutingPersistChain;
    var body = getManualMatrixProbe().buildCommand(
      _modelRoutingProviderContext(providerId), _stgModelRoutingSavedJson,
      { attempts: _stgMatrixAttempts[providerId] || 3, force: force, only: only });
    var snap = await Api.modelRouting.probeCellsStart(providerId, body);
    if (!_ingestProbeSnapshot(providerId, snap)) throw new Error(t('settings.matrixProbeFailed'));
    _rerenderMatrix(providerId);
    if (_stgMatrixProbe[providerId].status === 'running') _pollMatrixProbe(providerId);
  } catch (e) {
    var message = getManualMatrixProbe().startError(e);
    delete _stgMatrixProbeScope[providerId];
    _stgMatrixProbe[providerId] = { status: 'error', cells: {}, summary: { ok: 0, disable: 0 }, error: message, start_error: true };
    if (typeof showToast === 'function') showToast(message, 'error');
    _rerenderMatrix(providerId);
  }
}

/** Poll a running probe until it reaches a terminal state. */
function _pollMatrixProbe(providerId) {
  if (_stgMatrixProbeTimers[providerId]) clearTimeout(_stgMatrixProbeTimers[providerId]);
  _stgMatrixProbeTimers[providerId] = setTimeout(function tick() {
    // Settings closed → stop polling; _resumeMatrixProbe re-attaches on reopen.
    if (!document.getElementById('stgProviderList')) {
      delete _stgMatrixProbeTimers[providerId];
      _stgMatrixProbeAttached[providerId] = false;
      return;
    }
    Api.modelRouting.probeCellsStatus(providerId).then(function(snap) {
      _ingestProbeSnapshot(providerId, snap);
      _rerenderMatrix(providerId);
      if (snap && snap.status === 'running') {
        _stgMatrixProbeTimers[providerId] = setTimeout(tick, 1500);
      } else {
        delete _stgMatrixProbeTimers[providerId];
      }
    }).catch(function() {
      _stgMatrixProbeTimers[providerId] = setTimeout(tick, 3000);
    });
  }, 1500);
}

/** Re-attach to a persisted/running probe on (re)opening the matrix. */
function _resumeMatrixProbe(providerId) {
  // Don't clobber a live local run.
  if (_stgMatrixProbe[providerId] && _stgMatrixProbe[providerId].status === 'running'
      && _stgMatrixProbeTimers[providerId]) return;
  if (typeof Api === 'undefined' || !Api.modelRouting
      || typeof Api.modelRouting.probeCellsStatus !== 'function') return;
  Api.modelRouting.probeCellsStatus(providerId).then(function(snap) {
    if (_ingestProbeSnapshot(providerId, snap)) {
      _rerenderMatrix(providerId);
      if (_stgMatrixProbe[providerId].status === 'running') _pollMatrixProbe(providerId);
    }
  }).catch(function() { /* best-effort resume */ });
}

/** Explicit draft edit: fold the probe's per-wire verdicts into the
 *  credential grants. Symmetric — recommend-disable cells add wire
 *  exclusions (dropping the model grant only when every probed wire of the
 *  model is flagged), and ok cells whose wire is withheld mint grants or
 *  lift exclusions. Probe ingestion never invokes this action; the Settings
 *  Save flow is still the only persistence boundary. */
/** Pure projection: how many (credential × model) grants would the current
 *  probe's actionable cells change? Groups cells exactly like the apply path
 *  and replays the same decision logic against a credential's grants — but
 *  never mutates. Both the toolbar count and the apply action derive from
 *  this so the number on the button always equals what Apply will do. */
function _matrixApplyPlan(context, probe) {
  if (!context || !probe || !probe.cells) return [];
  var byRoot = {};
  _matrixModelRows(context).forEach(function(entry) { byRoot[entry.canonical] = entry; });
  // Group actionable cells by (credential × model) so a model loses its
  // grant only when ALL its probed wires recommend-disable.
  var groups = {}; // credIdx|creator|model → { credential, entry, disables:[], enables:[] }
  Object.keys(probe.cells).forEach(function(k) {
    var c = probe.cells[k];
    if (!c) return;
    // Historical discovery evidence must not re-arm into grant REMOVALS —
    // but the legacy marker never carried an ok verdict, so enablement of a
    // currently-off wire must still apply. Ingestion already cleared
    // recommend_disable on these cells; this is defense in depth.
    if (c.deployment_enabled === false && c.recommend_disable) return;
    var entry = byRoot[c.root_model_id];
    if (!entry || entry.pending) return;
    var item = context.credentials[c.key_idx];
    if (!item) return;
    var disable = !!c.recommend_disable;
    var enable = (c.status === 'ok');
    if (!disable && !enable) return;
    // A non-chat model may only change on a verdict from its OWN modality
    // probe — never on a stale chat-completions verdict that cannot speak
    // for its real endpoint.
    if (_matrixModelIsNonChat(entry) && !_isFreshModalityVerdict(c)) return;
    var creator = String(entry.offering.model.creator_id || '');
    var modelId = String(entry.offering.model.model_id || '');
    var key = c.key_idx + '|' + creator + '|' + modelId;
    var g = groups[key] || (groups[key] = {
      credential: item.row, entry: entry, creator: creator, modelId: modelId,
      disables: [], enables: []
    });
    (disable ? g.disables : g.enables).push(String(c.model_id));
  });

  var plan = [];
  Object.keys(groups).forEach(function(key) {
    var g = groups[key];
    var credential = g.credential;
    var grants = (credential.authorization && credential.authorization.models) || [];
    var grant = null;
    for (var i = 0; i < grants.length; i++) {
      if (String(grants[i].creator_id || '') === g.creator &&
          String(grants[i].model_id || '') === g.modelId) { grant = grants[i]; break; }
    }
    var pool = _matrixRowPool(g.entry).map(String);
    var disabledAll = g.disables.length > 0 && g.enables.length === 0 &&
      pool.every(function(id) { return g.disables.indexOf(id) >= 0; });
    if (disabledAll) {
      if (grant) plan.push({ group: g, kind: 'drop' });
      return;
    }
    if (!grant) {
      if (g.enables.length === 0) return;
      plan.push({ group: g, kind: 'mint' });
      return;
    }
    var excluded = (grant.excluded_wire_model_ids || []).slice();
    var before = excluded.join('\u0001');
    g.disables.forEach(function(id) { if (excluded.indexOf(id) < 0) excluded.push(id); });
    g.enables.forEach(function(id) {
      var p = excluded.indexOf(id); if (p >= 0) excluded.splice(p, 1);
    });
    if (excluded.join('\u0001') !== before) {
      plan.push({ group: g, kind: 'retune', excluded: excluded });
    }
  });
  return plan;
}

/** Count of grants Apply would change — drives the toolbar button. */
function _matrixApplyCount(providerId) {
  var context = _modelRoutingProviderContext(providerId);
  var probe = _stgMatrixProbe[providerId];
  if (!context || !probe || probe.status !== 'done' ||
      (probe.total && probe.done_count !== probe.total)) return 0;
  return _matrixApplyPlan(context, probe).length;
}

function _applyMatrixRecommendations(providerId) {
  var context = _modelRoutingProviderContext(providerId);
  var probe = _stgMatrixProbe[providerId];
  if (!context || !probe || !probe.cells) return;
  // Enablement now flows from ok verdicts, so apply must wait for a COMPLETE
  // probe — a partial/running/errored snapshot must never mint or lift a
  // grant on half the evidence. Mirrors the backend's incomplete-probe guard.
  if (probe.status !== 'done' ||
      (probe.total && probe.done_count !== probe.total)) return;

  var applied = 0;
  _matrixApplyPlan(context, probe).forEach(function(action) {
    var g = action.group;
    var credential = g.credential;
    if (!credential.authorization) credential.authorization = { connection_ids: [], models: [] };
    var grants = credential.authorization.models || (credential.authorization.models = []);
    if (action.kind === 'drop') {
      credential.authorization.models = grants.filter(function(ref) {
        return !(String(ref.creator_id || '') === g.creator && String(ref.model_id || '') === g.modelId);
      });
      applied++;
      return;
    }
    if (action.kind === 'mint') {
      var fresh = { creator_id: g.creator, model_id: g.modelId };
      if (g.disables.length) fresh.excluded_wire_model_ids = g.disables.slice();
      grants.push(fresh);
      applied++;
      return;
    }
    // retune: rewrite the existing grant's exclusions in place.
    var grant = null;
    for (var i = 0; i < grants.length; i++) {
      if (String(grants[i].creator_id || '') === g.creator &&
          String(grants[i].model_id || '') === g.modelId) { grant = grants[i]; break; }
    }
    if (!grant) return;
    if (action.excluded.length) grant.excluded_wire_model_ids = action.excluded;
    else delete grant.excluded_wire_model_ids;
    applied++;
  });

  if (typeof showToast === 'function') {
    showToast(applied > 0
      ? t('settings.matrixApplied').replace('{disabled}', String(applied))
      : t('settings.matrixNothingApplied'), applied > 0 ? 'success' : 'info');
  }
  _rerenderMatrix(providerId);
}

/** Hide probe results locally for this session (disk snapshot is kept;
 *  re-opening Settings re-attaches via _resumeMatrixProbe). */
function _clearMatrixProbe(providerId) {
  if (_stgMatrixProbeTimers[providerId]) {
    clearTimeout(_stgMatrixProbeTimers[providerId]);
    delete _stgMatrixProbeTimers[providerId];
  }
  delete _stgMatrixProbe[providerId];
  delete _stgMatrixProbeScope[providerId];
  _stgMatrixProbeAttached[providerId] = true; // don't auto-reattach until reopen
  _rerenderMatrix(providerId);
}
/* ===== migrated source: widgets/chip_input.js ===== */
/* ═══════════════════════════════════════════════════════════════════
   settings/chip_input.js — Reusable tag/chip input for lists of short
   strings (domains, etc.). Replaces the cramped multi-line <textarea>
   that scrolled awkwardly for domain lists.

   Usage:
     ChipInput.init('settingSkipDomains', ['youtube.com', 'x.com']);
     var domains = ChipInput.getValues('settingSkipDomains');

   The container element must exist with the given id and class
   `chip-input`. Values are deduped + trimmed; blank entries dropped.
   Adding accepts Enter / comma / blur, and a pasted multi-line/comma
   blob is split into many chips at once.

   Concatenated by Vite's module graph — shared window scope, no imports.
   ═══════════════════════════════════════════════════════════════════ */

runtimeScope.ChipInput = (function () {
  var _store = {};   // containerId -> array of values

  function _normalize(list) {
    var seen = {};
    var out = [];
    (list || []).forEach(function (v) {
      var s = String(v == null ? '' : v).trim();
      if (!s || seen[s]) return;
      seen[s] = true;
      out.push(s);
    });
    return out;
  }

  function _render(id) {
    var box = document.getElementById(id);
    if (!box) return;
    var values = _store[id] || [];
    var placeholder = box.getAttribute('data-placeholder') || '';
    var chips = values.map(function (v, i) {
      return String(safeHtml`<span class="chip"><span class="chip-text">${v}</span>`) +
        String(safeHtml`<button type="button" class="chip-x icon-box" title="${t('common.remove') || '移除'}"
          data-tofu-action="ChipInput.remove('${raw(id)}', ${raw(String(i))})"><svg width="10" height="10" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" aria-hidden="true"><path d="M6 6l12 12M18 6L6 18"/></svg></button></span>`);
    });
    box.innerHTML = chips.join('') + safeHtml`<input type="text" class="chip-add"
        placeholder="${placeholder}"
        data-tofu-action-keydown="ChipInput._onKey(event, '${raw(id)}')"
        data-tofu-action-blur="ChipInput._onBlur('${raw(id)}')"
        data-tofu-action-paste="ChipInput._onPaste(event, '${raw(id)}')">`;
  }

  function _splitBlob(text) {
    return String(text || '').split(/[\s,;]+/);
  }

  function init(id, values) {
    _store[id] = _normalize(values);
    _render(id);
  }

  function getValues(id) {
    return (_store[id] || []).slice();
  }

  function add(id, raw) {
    var additions = _splitBlob(raw);
    var cur = _store[id] || [];
    _store[id] = _normalize(cur.concat(additions));
    _render(id);
    // Keep focus on the add-field for fast multi-entry.
    var box = document.getElementById(id);
    var input = box && box.querySelector('.chip-add');
    if (input) input.focus();
  }

  function remove(id, index) {
    var cur = _store[id] || [];
    cur.splice(index, 1);
    _store[id] = cur;
    _render(id);
  }

  function _onKey(ev, id) {
    if (ev.key === 'Enter' || ev.key === ',') {
      ev.preventDefault();
      add(id, ev.target.value);
      ev.target.value = '';
    } else if (ev.key === 'Backspace' && !ev.target.value) {
      // Backspace on empty field removes the last chip.
      var cur = _store[id] || [];
      if (cur.length) remove(id, cur.length - 1);
    }
  }

  function _onBlur(id) {
    var box = document.getElementById(id);
    var input = box && box.querySelector('.chip-add');
    if (input && input.value.trim()) {
      add(id, input.value);
      input.value = '';
    }
  }

  function _onPaste(ev, id) {
    var text = (ev.clipboardData || /** @type {any} */ (window).clipboardData).getData('text');
    if (text && /[\s,;\n]/.test(text)) {
      ev.preventDefault();
      add(id, text);
      ev.target.value = '';
    }
  }

  return { init: init, getValues: getValues, add: add, remove: remove,
           _onKey: _onKey, _onBlur: _onBlur, _onPaste: _onPaste };
})();

/* ===== migrated source: settings/other_tabs.js ===== */
/* ═══════════════════════════════════════════════════════════════════
   settings/other tabs — extracted from settings.js (split 2026-05-28)

   Other settings tabs: Search, Network, MT-provider, Feishu, Advanced + cache stats.

   This file is concatenated by Vite's module graph — symbols share
   the same window scope as every other frontend/src/runtime/*.js file. No
   exports / imports needed.
   ═══════════════════════════════════════════════════════════════════ */

// ══════════════════════════════════════════════════════
//  Search / Advanced tabs
// ══════════════════════════════════════════════════════

function _populateSearchTab(cfg) {
  var s = cfg.search || {};
  var overrides = (s.overrides && typeof s.overrides === 'object') ? s.overrides : {};
  _searchLastPreset = s.profile || 'balanced';
  // Saved overrides ⇔ the select's 自定义 option — one source of truth.
  _setVal('settingSearchProfile', Object.keys(overrides).length > 0 ? 'custom' : _searchLastPreset);
  var cb = document.getElementById('settingLlmContentFilter');
  if (cb) cb.checked = s.llm_content_filter !== false;  // default: on
  _setVal('settingFetchTopN', s.fetch_top_n || 6);
  _setVal('settingFetchTimeout', s.fetch_timeout || 15);
  _setVal('settingMaxCharsSearch', s.max_chars_search || 60000);
  _setVal('settingMaxCharsDirect', s.max_chars_direct || 200000);
  _setVal('settingMaxCharsPdf', s.max_chars_pdf || 0);
  var deepen = document.getElementById('settingSearchDeepen');
  if (deepen) deepen.checked = s.deepen_enabled === true;
  // Max download size is stored in BYTES but displayed in MB — humans do not
  // think in 20971520. Save converts back (save_export.js).
  _setVal('settingMaxBytesMB', _bytesToMB(s.max_bytes || 20971520));
  if (typeof runtimeScope.ChipInput !== 'undefined') runtimeScope.ChipInput.init('settingSkipDomains', s.skip_domains || []);
  _wireSearchPipelinePreview();
  _syncSearchOverrideControls();
  _renderSearchBrowserAccess();
  if (typeof _renderAuthSources === 'function') _renderAuthSources();
  if (typeof _renderPrivateHosts === 'function') _renderPrivateHosts();
  if (typeof runtimeScope._renderSitePacks === 'function') runtimeScope._renderSitePacks();
}

// Last real (non-custom) preset — the wire never carries profile='custom':
// saving 自定义 sends this preset as the base plus the four-knob overrides.
var _searchLastPreset = 'balanced';

var _SEARCH_PROFILE_PRESETS = {
  fast: { fetch_top_n: 3, max_chars_search: 30000,
          llm_content_filter: false, deepen_enabled: false },
  balanced: { fetch_top_n: 6, max_chars_search: 60000,
              llm_content_filter: true, deepen_enabled: false },
  deep: { fetch_top_n: 10, max_chars_search: 100000,
          llm_content_filter: true, deepen_enabled: true }
};

function _searchProfileChanged() {
  var profile = (document.getElementById('settingSearchProfile') || {}).value || 'balanced';
  if (profile !== 'custom') {
    _searchLastPreset = profile;
    var p = _SEARCH_PROFILE_PRESETS[profile] || _SEARCH_PROFILE_PRESETS.balanced;
    _setVal('settingFetchTopN', p.fetch_top_n);
    _setVal('settingMaxCharsSearch', p.max_chars_search);
    var filter = document.getElementById('settingLlmContentFilter');
    var deepen = document.getElementById('settingSearchDeepen');
    if (filter) filter.checked = p.llm_content_filter;
    if (deepen) deepen.checked = p.deepen_enabled;
  }
  _syncSearchOverrideControls();
  _refreshSearchPipelinePreview();
}

function _syncSearchOverrideControls() {
  var sel = document.getElementById('settingSearchProfile');
  // A legacy/embedded settings surface has no profile select — its concrete
  // knobs stay live; only the new profile UI gates them on 自定义.
  var custom = !sel || sel.value === 'custom';
  document.querySelectorAll('.search-override-control input').forEach(function (el) {
    el.disabled = !custom;
  });
}

function _renderSearchBrowserAccess() {
  var owner = runtimeScope._renderSearchBrowserAccessOwner;
  if (typeof owner === 'function') return owner();
  var statusBox = document.getElementById('searchBrowserStatus');
  if (statusBox) {
    statusBox.innerHTML = '<span class="search-status-badge off">' +
      escapeHtml(t('settings.browserStatusUnavailable')) + '</span>';
  }
}

async function _testSearchBrowser() {
  var owner = runtimeScope._testSearchBrowserOwner;
  if (typeof owner === 'function') return owner();
}

async function _browserAccessDenyRead() {
  var owner = runtimeScope._browserAccessDenyReadOwner;
  if (typeof owner === 'function') return owner();
}

/** bytes → MB for display (round to 1 decimal, trim trailing .0). */
function _bytesToMB(bytes) {
  var mb = (parseInt(bytes, 10) || 0) / 1048576;
  var rounded = Math.round(mb * 10) / 10;
  return (rounded === Math.floor(rounded)) ? Math.floor(rounded) : rounded;
}

/** The pipeline preview says in one sentence what the backend WILL DO with
 *  the current knob values — the frontend↔backend bridge. Live-updates as
 *  the user edits the inputs (wired once). */
function _refreshSearchPipelinePreview() {
  var el = document.getElementById('searchPipelinePreview');
  if (!el) return;
  var _v = function (id, dflt) {
    var n = parseInt((document.getElementById(id) || {}).value, 10);
    return (isNaN(n) ? dflt : n);
  };
  var n = _v('settingFetchTopN', 6);
  var timeout = _v('settingFetchTimeout', 15);
  var chars = _v('settingMaxCharsSearch', 60000);
  var filterCb = document.getElementById('settingLlmContentFilter');
  var filterOn = filterCb ? filterCb.checked : true;
  var filterTxt = filterOn
    ? (t('settings.searchFilterOnTpl') || 'LLM 过滤杂质')
    : (t('settings.searchFilterOffTpl') || '跳过过滤（原文直送）');
  el.textContent = (t('settings.searchPipelineTpl') ||
    '搜索引擎返回结果 → 抓取前 {n} 个网页（每页 ≤{chars} 字符 · 超时 {timeout}s）→ {filter} → 注入对话')
    .replace('{n}', n).replace('{chars}', (chars || 0).toLocaleString('en-US'))
    .replace('{timeout}', timeout).replace('{filter}', filterTxt);
  el.classList.toggle('filter-off', !filterOn);
}

function _wireSearchPipelinePreview() {
  _refreshSearchPipelinePreview();
  if (/** @type {any} */ (_wireSearchPipelinePreview)._done) return;
  /** @type {any} */ (_wireSearchPipelinePreview)._done = true;
  ['settingFetchTopN', 'settingFetchTimeout', 'settingMaxCharsSearch',
   'settingLlmContentFilter'].forEach(function (id) {
    var el = document.getElementById(id);
    if (el) {
      el.addEventListener('input', _refreshSearchPipelinePreview);
      el.addEventListener('change', _refreshSearchPipelinePreview);
    }
  });
}

// ══════════════════════════════════════════════════════
//  Network tab (proxy bypass)
// ══════════════════════════════════════════════════════

function _populateNetworkTab(cfg) {
  var n = cfg.network || {};

  // ── Proxy pool editor (ordered, scoped) ──
  _renderProxyPool(n);

  // Show env hint banner if env vars are set (so user knows the baseline)
  var envParts = [];
  if (n.env_http_proxy) envParts.push('http_proxy=' + n.env_http_proxy);
  if (n.env_https_proxy && n.env_https_proxy !== n.env_http_proxy)
    envParts.push('https_proxy=' + n.env_https_proxy);

  var envBanner = document.getElementById('proxyEnvBanner');
  var envBannerText = document.getElementById('proxyEnvBannerText');
  if (envBanner && envBannerText && envParts.length > 0) {
    envBanner.style.display = '';
    envBannerText.textContent = t('settings.proxyEnvBanner', { vars: envParts.join(' · ') });
  } else if (envBanner) {
    envBanner.style.display = 'none';
  }

  // ── Unified bypass addresses (every row is directly editable) ──
  _renderProxyBypassDomains(n.proxy_bypass_domains || []);

  // Show hint if env var PROXY_BYPASS_DOMAINS is set
  var hint = document.getElementById('proxyEnvHint');
  var hintText = document.getElementById('proxyEnvHintText');
  if (hint && hintText && n.env_proxy_bypass) {
    hint.style.display = '';
    hintText.textContent = t('settings.proxyEnvHint', { val: n.env_proxy_bypass });
  } else if (hint) {
    hint.style.display = 'none';
  }
}


// ── Direct-connection exceptions ──────────────────────────────────────────

function _renderProxyBypassDomains(values) {
  var list = document.getElementById('proxyBypassList');
  if (!list) return;
  var items = Array.isArray(values) && values.length ? values : [''];
  list.innerHTML = items.map(_proxyBypassRowHtml).join('');
  _proxyBypassRefreshOrder();
  var addBtn = document.getElementById('proxyBypassAddBtn');
  if (addBtn && !addBtn._wired) {
    addBtn._wired = true;
    addBtn.innerHTML = (typeof Icon === 'function' ? Icon('plus', 12) + ' ' : '') +
      escapeHtml(t('settings.proxyBypassAdd') || '添加直连地址');
    addBtn.onclick = _proxyBypassAppendRow;
  }
}

function _proxyBypassRowHtml(value, index) {
  var trash = (typeof Icon === 'function') ? Icon('trash', 12) : '×';
  return String(safeHtml`
    <div class="proxy-bypass-row">
      <span class="pb-order">${String((index == null ? 0 : index) + 1).padStart(2, '0')}</span>
      <input type="text" class="pb-value settings-mono" value="${value || ''}"
             placeholder="${t('settings.proxyBypassRowPlaceholder') || '.internal.example.com 或完整 URL'}"
             spellcheck="false" autocomplete="off" data-tofu-action-input="_proxyBypassRefreshCount()">
      <button type="button" class="auth-src-btn ghost danger sm icon-box pb-delete-btn"
              title="${t('settings.proxyBypassDelete') || '删除此项'}"
              data-tofu-action="_proxyBypassDelete(this)">${raw(trash)}</button>
    </div>`);
}

function _proxyBypassAppendRow() {
  var list = document.getElementById('proxyBypassList');
  if (!list) return;
  var rows = list.querySelectorAll('.proxy-bypass-row');
  var only = rows.length === 1 && !(rows[0].querySelector('.pb-value') || {}).value;
  if (!only) list.insertAdjacentHTML('beforeend', _proxyBypassRowHtml('', rows.length));
  _proxyBypassRefreshOrder();
  var inputs = list.querySelectorAll('.pb-value');
  if (inputs.length) inputs[inputs.length - 1].focus();
}

function _proxyBypassDelete(btn) {
  var row = btn && btn.closest ? btn.closest('.proxy-bypass-row') : null;
  var list = row && row.parentNode;
  if (!row || !list) return;
  row.remove();
  if (!list.querySelector('.proxy-bypass-row')) {
    list.insertAdjacentHTML('beforeend', _proxyBypassRowHtml('', 0));
  }
  _proxyBypassRefreshOrder();
}

function _proxyBypassRefreshOrder() {
  var rows = document.querySelectorAll('#proxyBypassList .proxy-bypass-row');
  for (var i = 0; i < rows.length; i++) {
    var order = rows[i].querySelector('.pb-order');
    if (order) order.textContent = String(i + 1).padStart(2, '0');
  }
  _proxyBypassRefreshCount();
}

function _proxyBypassRefreshCount() {
  var countEl = document.getElementById('proxyBypassCount');
  if (!countEl) return;
  var inputs = document.querySelectorAll('#proxyBypassList .pb-value');
  var count = 0;
  for (var i = 0; i < inputs.length; i++) {
    if ((inputs[i].value || '').trim()) count += 1;
  }
  countEl.textContent = (t('settings.proxyBypassCount') || '{count} 项')
    .replace('{count}', String(count));
}

function _collectProxyBypassDomains() {
  var list = document.getElementById('proxyBypassList');
  if (!list) return null;
  var out = [];
  var inputs = list.querySelectorAll('.pb-value');
  for (var i = 0; i < inputs.length; i++) {
    var value = (inputs[i].value || '').trim();
    if (value) out.push(value);
  }
  return out;
}


// ══════════════════════════════════════════════════════
//  Network tab — proxy pool editor (ordered, scoped, 2026-08-07)
// ══════════════════════════════════════════════════════
// Rows render from cfg.network.proxy_pool (credential-free; the backend
// holds user:pass in the credentials vault). A legacy single-proxy config
// (proxy_config) surfaces as ONE synthetic global row so saving migrates
// it into the pool seamlessly. All entry points null-guard the container
// so stripped-down jsdom harnesses (and a stale cached DOM) never crash.

function _renderProxyPool(n) {
  var list = document.getElementById('proxyPoolList');
  if (!list) return;
  var pool = ((n && n.proxy_pool) || []).slice();
  // Legacy migration: a configured legacy single proxy with no global pool
  // row rides the editor as one synthetic global row; saving retires the
  // legacy slot server-side.
  var hasGlobal = pool.some(function (e) { return e && e.scope === 'global'; });
  if (n && n.proxy_configured && n.http_proxy && !hasGlobal) {
    pool.push({
      id: 'legacy', name: (t('settings.proxyLegacyRow') || '旧版单代理（迁移）'),
      url: n.http_proxy, scope: 'global', enabled: true,
      has_credential: false, credential_vault: '', _legacy: true,
    });
  }
  list.innerHTML = pool.length
    ? pool.map(_proxyPoolRowHtml).join('')
    : String(safeHtml`<div class="proxy-pool-empty">
        <strong>${t('settings.proxyPoolEmpty') || '还没有代理'}</strong>
        <span>${t('settings.proxyPoolEmptyHint') || '不添加时，将继续使用系统环境变量中的代理配置。'}</span>
      </div>`);
  _proxyPoolRefreshOrder();
  if (runtimeScope.TofuDropdown) runtimeScope.TofuDropdown.enhance(list);
  var addBtn = document.getElementById('proxyPoolAddBtn');
  if (addBtn && !addBtn._wired) {
    addBtn._wired = true;
    addBtn.innerHTML = (typeof Icon === 'function' ? Icon('plus', 12) + ' ' : '') +
      escapeHtml(t('settings.proxyPoolAdd') || '添加代理');
    addBtn.onclick = function () { _proxyPoolAppendRow(); };
  }
}

function _proxyPoolRowHtml(e, index) {
  var id = e.id || '';
  var scope = e.scope === 'global' ? 'global' : 'subscription';
  var hasCred = !!(e.has_credential || e.credential_vault);
  var order = String((index == null ? 0 : index) + 1).padStart(2, '0');
  var displayName = e.name || ((t('settings.proxyCardTitle') || '代理') + ' ' + order);
  var hostLabel = _proxyPoolDisplayHost(e.url || '') || (t('settings.proxyUrlEmpty') || '尚未填写链接');
  var scopeLabel = scope === 'global'
    ? (t('settings.proxyScopeGlobal') || '全部出站流量')
    : (t('settings.proxyScopeSub') || '仅订阅流量');
  var trash = (typeof Icon === 'function') ? Icon('trash', 12) : '×';
  var eye = (typeof Icon === 'function') ? Icon('eye', 14) : '◉';
  var edit = (typeof Icon === 'function') ? Icon('edit', 13) : '✎';
  var chevron = (typeof Icon === 'function') ? Icon('chevronDown', 12) : '⌄';
  return String(safeHtml`
    <div class="proxy-pool-row" data-id="${id}" data-has-credential="${hasCred ? '1' : '0'}" data-url-dirty="0">
      <div class="proxy-pool-head">
        <div class="proxy-pool-identity">
          <span class="pp-order">${order}</span>
          <div class="pp-identity-copy">
            <strong class="pp-display-name" title="${displayName}">${displayName}</strong>
            <span class="pp-display-host" title="${hostLabel}">${hostLabel}</span>
          </div>
          <span class="pp-scope-badge">${scopeLabel}</span>
        </div>
        <label class="pp-enabled-control">
          <input type="checkbox" class="pp-enabled" ${raw(e.enabled !== false ? 'checked' : '')}>
          <span class="pp-switch-track"><span></span></span>
          <span class="pp-enabled-text">${t('settings.proxyEnable') || '启用'}</span>
        </label>
        <button type="button" class="pp-edit-toggle"
                title="${t('settings.proxyEdit') || '编辑代理'}"
                aria-label="${t('settings.proxyEdit') || '编辑代理'}" aria-expanded="false"
                data-tofu-action="_proxyPoolToggleEditor(this)">${raw(edit)}</button>
      </div>

      <div class="proxy-pool-detail" hidden>
        <div class="pp-detail-grid">
          <label class="pp-field pp-field-name">
            <span>${t('settings.proxyNameLabel') || '名称（可选）'}</span>
            <input type="text" class="pp-name" value="${e.name || ''}"
                   placeholder="${t('settings.proxyPhName') || '例如：公司出口'}" maxlength="40"
                   data-tofu-action-input="_proxyPoolSyncMeta(this)">
          </label>
          <label class="pp-field pp-field-scope">
            <span>${t('settings.proxyScopeLabel') || '使用范围'}</span>
            <select class="pp-scope" data-tofu-dd data-tofu-action-change="_proxyPoolSyncMeta(this)">
              <option value="subscription" ${raw(scope === 'subscription' ? 'selected' : '')}>${t('settings.proxyScopeSub') || '仅订阅流量'}</option>
              <option value="global" ${raw(scope === 'global' ? 'selected' : '')}>${t('settings.proxyScopeGlobal') || '全部出站流量'}</option>
            </select>
          </label>
        </div>
        <label class="pp-url-field">
          <span class="pp-url-label">
            <strong>${t('settings.proxyUrlLabel') || '代理链接'}</strong>
            <span class="pp-url-state">${hasCred ? (t('settings.proxyUrlProtected') || '敏感信息已隐藏') : 'HTTP / HTTPS'}</span>
          </span>
          <span class="pp-url-input">
            <input type="${hasCred ? 'password' : 'text'}" class="pp-url settings-mono" value="${e.url || ''}"
                   placeholder="http://host:port" spellcheck="false" autocomplete="off"
                   data-tofu-action-input="_proxyPoolUrlChanged(this)">
            <button type="button" class="pp-url-visibility"
                    title="${hasCred ? (t('settings.proxyShowUrl') || '显示完整链接') : (t('settings.proxyHideUrl') || '隐藏链接')}"
                    aria-label="${hasCred ? (t('settings.proxyShowUrl') || '显示完整链接') : (t('settings.proxyHideUrl') || '隐藏链接')}"
                    data-tofu-action="_proxyPoolToggleUrlVisibility(this)">${raw(eye)}</button>
          </span>
          <span class="pp-url-help">${t('settings.proxyUrlHelp') || '复制完整链接后直接粘贴，账号密码会自动安全保存。'}</span>
        </label>

        <div class="proxy-pool-card-footer">
          <div class="pp-reorder" aria-label="${t('settings.proxyReorder') || '调整顺序'}">
            <span>${t('settings.proxyOrderLabel') || '优先级'}</span>
            <button type="button" class="auth-src-btn ghost sm icon-box pp-move pp-move-up"
                    title="${t('settings.proxyMoveUp') || '上移'}" data-tofu-action="_proxyPoolMove(this, -1)">${raw(chevron)}</button>
            <button type="button" class="auth-src-btn ghost sm icon-box pp-move pp-move-down"
                    title="${t('settings.proxyMoveDown') || '下移'}" data-tofu-action="_proxyPoolMove(this, 1)">${raw(chevron)}</button>
          </div>
          <div class="pp-card-actions">
            <button type="button" class="auth-src-btn sm pp-test-btn" data-tofu-action="_proxyPoolTest(this)">${t('settings.proxyTest') || '测试连接'}</button>
            <button type="button" class="auth-src-btn ghost danger sm pp-delete-btn" title="${t('settings.proxyDel') || '删除'}"
                    data-tofu-action="_proxyPoolDelete(this)">${raw(trash)} <span>${t('settings.proxyDel') || '删除'}</span></button>
          </div>
        </div>
        <div class="pp-result" style="display:none"></div>
      </div>
      <input type="hidden" class="pp-credvault" value="${e.credential_vault || ''}">
      <input type="hidden" class="pp-clearcred" value="0">
    </div>`);
}

function _proxyPoolDisplayHost(url) {
  var value = String(url || '').trim();
  if (!value) return '';
  try {
    var parsed = new URL(value.indexOf('://') === -1 ? 'http://' + value : value);
    return parsed.host || value;
  } catch (_) {
    return value.replace(/^\w+:\/\//, '').replace(/^.*@/, '').split('/')[0];
  }
}

function _proxyPoolUrlHasCredential(url) {
  var value = String(url || '').trim();
  if (!value) return false;
  try {
    var parsed = new URL(value.indexOf('://') === -1 ? 'http://' + value : value);
    return !!(parsed.username || parsed.password);
  } catch (_) {
    return /:\/\/[^/@]+@/.test(value);
  }
}

function _proxyPoolUrlChanged(input) {
  var row = input && input.closest ? input.closest('.proxy-pool-row') : null;
  if (!row) return;
  row.setAttribute('data-url-dirty', '1');
  var sensitive = _proxyPoolUrlHasCredential(input.value);
  input.type = sensitive ? 'password' : 'text';
  var state = row.querySelector('.pp-url-state');
  if (state) state.textContent = sensitive
    ? (t('settings.proxyUrlProtected') || '敏感信息已隐藏')
    : 'HTTP / HTTPS';
  var visibility = row.querySelector('.pp-url-visibility');
  if (visibility) {
    visibility.title = sensitive
      ? (t('settings.proxyShowUrl') || '显示完整链接')
      : (t('settings.proxyHideUrl') || '隐藏链接');
    visibility.setAttribute('aria-label', visibility.title);
  }
  var host = row.querySelector('.pp-display-host');
  var hostLabel = _proxyPoolDisplayHost(input.value) ||
    (t('settings.proxyUrlEmpty') || '尚未填写链接');
  if (host) { host.textContent = hostLabel; host.title = hostLabel; }
}

function _proxyPoolAttachCredential(url, secret) {
  var rawSecret = String(secret || '');
  var pivot = rawSecret.indexOf(':');
  var username = pivot < 0 ? rawSecret : rawSecret.slice(0, pivot);
  var password = pivot < 0 ? '' : rawSecret.slice(pivot + 1);
  if (!username) return url;
  try {
    var parsed = new URL(String(url || '').indexOf('://') === -1 ? 'http://' + url : url);
    var userinfo = encodeURIComponent(username);
    if (password) userinfo += ':' + encodeURIComponent(password);
    return parsed.protocol + '//' + userinfo + '@' + parsed.host;
  } catch (_) {
    return url;
  }
}

function _proxyPoolToggleUrlVisibility(btn) {
  var row = btn && btn.closest ? btn.closest('.proxy-pool-row') : null;
  var input = row && row.querySelector('.pp-url');
  if (!row || !input) return;
  if (input.type === 'text') {
    input.type = 'password';
    btn.title = t('settings.proxyShowUrl') || '显示完整链接';
    btn.setAttribute('aria-label', btn.title);
    return;
  }
  var vault = (row.querySelector('.pp-credvault') || {}).value || '';
  if (_proxyPoolUrlHasCredential(input.value) || !vault) {
    input.type = 'text';
    btn.title = t('settings.proxyHideUrl') || '隐藏链接';
    btn.setAttribute('aria-label', btn.title);
    return;
  }
  var feedback = row.querySelector('.pp-url-help');
  if (typeof Api === 'undefined' || !Api.credentials || !Api.credentials.reveal) {
    if (feedback) feedback.textContent = t('settings.proxyRevealFail') || '暂时无法读取完整链接';
    return;
  }
  btn.disabled = true;
  if (feedback) feedback.textContent = t('settings.proxyRevealing') || '正在读取完整链接…';
  Api.credentials.reveal(vault).then(function (res) {
    btn.disabled = false;
    input.value = _proxyPoolAttachCredential(input.value, (res && res.value) || '');
    input.type = 'text';
    btn.title = t('settings.proxyHideUrl') || '隐藏链接';
    btn.setAttribute('aria-label', btn.title);
    if (feedback) feedback.textContent = t('settings.proxyUrlRevealed') || '完整链接已显示，30 秒后自动隐藏。';
    setTimeout(function () {
      if (input && input.isConnected) {
        input.type = 'password';
        btn.title = t('settings.proxyShowUrl') || '显示完整链接';
        btn.setAttribute('aria-label', btn.title);
        if (feedback) feedback.textContent = t('settings.proxyUrlHelp') || '复制完整链接后直接粘贴，账号密码会自动安全保存。';
      }
    }, 30000);
  }).catch(function (err) {
    btn.disabled = false;
    if (feedback) feedback.textContent = (err && err.message) ||
      (t('settings.proxyRevealFail') || '读取失败');
  });
}

function _proxyPoolSyncMeta(control) {
  var row = control && control.closest ? control.closest('.proxy-pool-row') : null;
  if (!row) return;
  var name = ((row.querySelector('.pp-name') || {}).value || '').trim();
  var order = (row.querySelector('.pp-order') || {}).textContent || '01';
  var display = row.querySelector('.pp-display-name');
  var displayName = name || ((t('settings.proxyCardTitle') || '代理') + ' ' + order);
  if (display) { display.textContent = displayName; display.title = displayName; }
  var scope = (row.querySelector('.pp-scope') || {}).value || 'subscription';
  var scopeLabel = scope === 'global'
    ? (t('settings.proxyScopeGlobal') || '全部出站流量')
    : (t('settings.proxyScopeSub') || '仅订阅流量');
  var badge = row.querySelector('.pp-scope-badge');
  if (badge) badge.textContent = scopeLabel;
}

function _proxyPoolSetEditor(row, open) {
  if (!row) return;
  var detail = row.querySelector('.proxy-pool-detail');
  var toggle = row.querySelector('.pp-edit-toggle');
  if (!detail || !toggle) return;
  row.classList.toggle('is-editing', !!open);
  detail.hidden = !open;
  toggle.setAttribute('aria-expanded', open ? 'true' : 'false');
  toggle.title = open
    ? (t('settings.proxyCloseEditor') || '收起编辑')
    : (t('settings.proxyEdit') || '编辑代理');
  toggle.setAttribute('aria-label', toggle.title);
  if (!open) {
    var urlInput = row.querySelector('.pp-url');
    if (urlInput && (_proxyPoolUrlHasCredential(urlInput.value) ||
        row.getAttribute('data-has-credential') === '1')) {
      urlInput.type = 'password';
    }
  }
}

function _proxyPoolToggleEditor(btn) {
  var row = btn && btn.closest ? btn.closest('.proxy-pool-row') : null;
  if (!row) return;
  var willOpen = !row.classList.contains('is-editing');
  var rows = document.querySelectorAll('#proxyPoolList .proxy-pool-row');
  for (var i = 0; i < rows.length; i++) {
    if (rows[i] !== row) _proxyPoolSetEditor(rows[i], false);
  }
  _proxyPoolSetEditor(row, willOpen);
}

function _proxyPoolAppendRow() {
  var list = document.getElementById('proxyPoolList');
  if (!list) return;
  var empty = list.querySelector('.proxy-pool-empty');
  if (empty) empty.remove();
  var count = list.querySelectorAll('.proxy-pool-row').length;
  list.insertAdjacentHTML('beforeend', _proxyPoolRowHtml({ enabled: true }, count));
  if (runtimeScope.TofuDropdown) runtimeScope.TofuDropdown.enhance(list);
  _proxyPoolRefreshOrder();
  var rows = list.querySelectorAll('.proxy-pool-row');
  var last = rows[rows.length - 1];
  var editToggle = last && last.querySelector('.pp-edit-toggle');
  if (editToggle) _proxyPoolToggleEditor(editToggle);
  var urlInput = last && last.querySelector('.pp-url');
  if (urlInput) urlInput.focus();
}

function _proxyPoolDelete(btn) {
  var row = btn && btn.closest ? btn.closest('.proxy-pool-row') : null;
  if (!row) return;
  var list = row.parentNode;
  row.remove();
  if (list && !list.querySelector('.proxy-pool-row')) {
    list.innerHTML = String(safeHtml`<div class="proxy-pool-empty">
      <strong>${t('settings.proxyPoolEmpty') || '还没有代理'}</strong>
      <span>${t('settings.proxyPoolEmptyHint') || '不添加时，将继续使用系统环境变量中的代理配置。'}</span>
    </div>`);
  }
  _proxyPoolRefreshOrder();
}

function _proxyPoolRefreshOrder() {
  var list = document.getElementById('proxyPoolList');
  if (!list) return;
  var rows = list.querySelectorAll('.proxy-pool-row');
  for (var i = 0; i < rows.length; i++) {
    var order = rows[i].querySelector('.pp-order');
    if (order) order.textContent = String(i + 1).padStart(2, '0');
    var up = rows[i].querySelector('.pp-move-up');
    var down = rows[i].querySelector('.pp-move-down');
    if (up) up.disabled = i === 0;
    if (down) down.disabled = i === rows.length - 1;
    _proxyPoolSyncMeta(rows[i].querySelector('.pp-name') || rows[i]);
  }
  var countEl = document.getElementById('proxyPoolCount');
  if (countEl) {
    countEl.textContent = (t('settings.proxyPoolCount') || '{count} 个代理')
      .replace('{count}', String(rows.length));
  }
}

function _proxyPoolMove(btn, delta) {
  var row = btn && btn.closest ? btn.closest('.proxy-pool-row') : null;
  if (!row || !row.parentNode) return;
  if (delta < 0 && row.previousElementSibling) {
    row.parentNode.insertBefore(row, row.previousElementSibling);
  } else if (delta > 0 && row.nextElementSibling) {
    row.parentNode.insertBefore(row.nextElementSibling, row);
  }
  _proxyPoolRefreshOrder();
}

function _proxyPoolRowPayload(row) {
  function val(sel) {
    var el = row.querySelector(sel);
    return (el && el.value || '').trim();
  }
  var enabledEl = row.querySelector('.pp-enabled');
  var url = val('.pp-url');
  var hadCredential = row.getAttribute('data-has-credential') === '1';
  var urlChanged = row.getAttribute('data-url-dirty') === '1';
  var clearCredential = hadCredential && urlChanged && !_proxyPoolUrlHasCredential(url);
  var clearEl = row.querySelector('.pp-clearcred');
  if (clearEl) clearEl.value = clearCredential ? '1' : '0';
  return {
    id: row.getAttribute('data-id') || '',
    name: val('.pp-name'),
    url: url,
    scope: (row.querySelector('.pp-scope') || {}).value || 'subscription',
    credential_vault: val('.pp-credvault'),
    clear_credential: clearCredential,
    enabled: enabledEl ? !!enabledEl.checked : true,
  };
}

function _proxyPoolTest(btn) {
  var row = btn && btn.closest ? btn.closest('.proxy-pool-row') : null;
  if (!row || typeof Api === 'undefined' || !Api.network) return;
  var result = row.querySelector('.pp-result');
  if (!result) return;
  var payload = _proxyPoolRowPayload(row);
  if (!payload.url) {
    result.style.display = '';
    result.className = 'pp-result err';
    result.textContent = t('settings.proxyTestNoUrl') || '先填写代理地址';
    return;
  }
  btn.disabled = true;
  result.style.display = '';
  result.className = 'pp-result';
  result.textContent = t('settings.proxyTesting') || '测试中…';
  Api.network.proxyTest(payload).then(function (data) {
    btn.disabled = false;
    var results = (data && data.results) || [];
    if (!results.length) {
      result.className = 'pp-result err';
      result.textContent = (data && data.error) || (t('settings.proxyTestFail') || '测试失败');
      return;
    }
    var ok = !!data.any_ok;
    result.className = 'pp-result ' + (ok ? 'ok' : 'err');
    result.textContent = results.map(function (r) {
      if (r.verdict === 'ok') {
        return (t('settings.proxyTestOkTpl') || '{label} 可达（HTTP {code} · {ms}ms）')
          .replace('{label}', r.label || r.target)
          .replace('{code}', String(r.status))
          .replace('{ms}', String(r.latency_ms));
      }
      if (r.verdict === 'geo_blocked') {
        return (t('settings.proxyTestBlockedTpl') || '{label} 被拦截（HTTP {code}）')
          .replace('{label}', r.label || r.target)
          .replace('{code}', String(r.status));
      }
      if (r.verdict === 'proxy_auth') {
        return (t('settings.proxyTestAuthTpl') || '{label} 代理认证失败（凭证失效或已过期）')
          .replace('{label}', r.label || r.target);
      }
      return (t('settings.proxyTestFailTpl') || '{label} 网络失败：{err}')
        .replace('{label}', r.label || r.target)
        .replace('{err}', r.error || 'timeout');
    }).join('　·　');
  }).catch(function (e) {
    btn.disabled = false;
    result.className = 'pp-result err';
    result.textContent = ((e && e.body && e.body.error) || (e && e.message) ||
      (t('settings.proxyTestFail') || '测试失败'));
  });
}

/** Collect the editor rows into the save payload. Returns null when the
 *  pool editor is absent from the DOM (legacy/other surfaces) so the caller
 *  leaves the server's proxy config untouched. */
function _collectProxyPool() {
  var list = document.getElementById('proxyPoolList');
  if (!list) return null;
  var out = [];
  var rows = list.querySelectorAll('.proxy-pool-row');
  for (var i = 0; i < rows.length; i++) {
    var p = _proxyPoolRowPayload(rows[i]);
    if (!p.url) continue;  // blank rows are dropped, never persisted
    out.push(p);
  }
  return out;
}

// ══════════════════════════════════════════════════════
//  Machine Translation Provider (General tab)
// ══════════════════════════════════════════════════════

function _populateMtProviderSection(cfg) {
  var mt = cfg.mt_provider || {};
  var enabledCb = document.getElementById('settingMtEnabled');
  var fieldsDiv = document.getElementById('mtProviderFields');
  if (enabledCb) {
    enabledCb.checked = !!mt.enabled;
    enabledCb.onchange = function() {
      if (fieldsDiv) fieldsDiv.style.display = this.checked ? '' : 'none';
    };
  }
  if (fieldsDiv) fieldsDiv.style.display = mt.enabled ? '' : 'none';

  var provider = mt.provider || 'niutrans';
  _setVal('settingMtProvider', provider);

  // NiuTrans fields (primary)
  _setVal('settingMtApiKey', provider === 'niutrans' ? (mt.api_key || '') : '');
  _setVal('settingMtAppId', provider === 'niutrans' ? (mt.app_id || '') : '');
  _setVal('settingMtApiUrl', provider === 'niutrans' ? (mt.api_url || '') : '');

  // Custom fields
  _setVal('settingMtApiKeyCustom', provider === 'custom' ? (mt.api_key || '') : '');
  _setVal('settingMtAppIdCustom', provider === 'custom' ? (mt.app_id || '') : '');
  _setVal('settingMtApiUrlCustom', provider === 'custom' ? (mt.api_url || '') : '');

  _switchMtProvider(provider);
}

/** Show/hide provider cards based on selection */
function _switchMtProvider(provider) {
  var niuCard = document.getElementById('mtCardNiutrans');
  var customCard = document.getElementById('mtCardCustom');
  if (niuCard) niuCard.style.display = provider === 'niutrans' ? '' : 'none';
  if (customCard) customCard.style.display = provider === 'custom' ? '' : 'none';
}

function _collectMtProviderConfig() {
  var enabledCb = document.getElementById('settingMtEnabled');
  var provider = (document.getElementById('settingMtProvider') || {}).value || 'niutrans';
  var suffix = provider === 'custom' ? 'Custom' : '';
  return {
    enabled: enabledCb ? enabledCb.checked : false,
    provider: provider,
    api_key: (document.getElementById('settingMtApiKey' + suffix) || {}).value || '',
    app_id: (document.getElementById('settingMtAppId' + suffix) || {}).value || '',
    api_url: (document.getElementById('settingMtApiUrl' + suffix) || {}).value || '',
  };
}

function _testMtProvider() {
  var provider = (document.getElementById('settingMtProvider') || {}).value || 'niutrans';
  var suffix = provider === 'custom' ? 'Custom' : '';
  var btn = document.getElementById('mtTestBtn' + suffix);
  var result = document.getElementById('mtTestResult' + suffix);
  if (!btn || !result) return;
  btn.disabled = true;
  result.textContent = t('settings.mtTesting');
  result.style.color = 'var(--text-secondary)';

  Api.translate.mtTest(_collectMtProviderConfig(),
    'Hello, this is a test of the machine translation service.'
  ).then(function(data) {
    btn.disabled = false;
    if (data && data.ok) {
      result.textContent = t('settings.mtTestOk') + (data.translated || '').substring(0, 60);
      result.style.color = 'var(--accent-green, #4caf50)';
    } else {
      result.textContent = '❌ ' + (data.error || t('settings.mtTestFail'));
      result.style.color = 'var(--accent-red, #f44336)';
    }
  }).catch(function(e) {
    btn.disabled = false;
    result.textContent = t('settings.mtTestReqFail') + e.message;
    result.style.color = 'var(--accent-red, #f44336)';
  });
}

// ══════════════════════════════════════════════════════
//  Feishu Bot settings (in General tab → Modules)
// ══════════════════════════════════════════════════════

/** Cached Feishu config for dirty-checking restart hint */
var _feishuOrigConfig = null;

function _populateFeishuTab(cfg) {
  var f = cfg.feishu || {};
  _feishuOrigConfig = JSON.parse(JSON.stringify(f));

  // Status dot
  var dot = document.getElementById('feishuStatusDot');
  var label = document.getElementById('feishuStatusLabel');
  var desc = document.getElementById('feishuStatusDesc');
  if (dot && label && desc) {
    if (f.connected) {
      dot.innerHTML = IconDot('green'); dot.title = t('settings.feishuConnected');
      desc.textContent = t('settings.feishuConnectedDesc', { app: (f.app_id_masked || '—') });
    } else if (f.enabled) {
      dot.innerHTML = IconDot('yellow'); dot.title = t('settings.feishuEnabledNotConnected');
      desc.textContent = t('settings.feishuCredsNotConnected');
    } else {
      dot.innerHTML = IconDot('grey'); dot.title = t('settings.feishuDisabled');
      desc.textContent = t('settings.feishuDisabledDesc');
    }
  }

  // Populate fields
  _setVal('settingFeishuAppId', f.app_id || '');
  // Don't populate secret — show placeholder instead
  var secretInput = document.getElementById('settingFeishuAppSecret');
  if (secretInput) {
    secretInput.value = '';
    secretInput.placeholder = f.has_secret ? t('settings.feishuSecretSaved') : t('settings.feishuSecretPlaceholder');
  }
  _setVal('settingFeishuDefaultProject', f.default_project || '');
  _setVal('settingFeishuWorkspaceRoot', f.workspace_root || '');
  var au = document.getElementById('settingFeishuAllowedUsers');
  if (au) au.value = (f.allowed_users || []).join('\n');

  // Restart hint on credential change
  var appIdInput = document.getElementById('settingFeishuAppId');
  if (appIdInput) {
    appIdInput.oninput = _checkFeishuRestartHint;
  }
  if (secretInput) {
    secretInput.oninput = _checkFeishuRestartHint;
  }
}

function _checkFeishuRestartHint() {
  var hint = document.getElementById('feishuRestartHint');
  if (!hint || !_feishuOrigConfig) return;
  var appId = (document.getElementById('settingFeishuAppId') || {}).value || '';
  var secret = (document.getElementById('settingFeishuAppSecret') || {}).value || '';
  var changed = appId !== (_feishuOrigConfig.app_id || '') || secret.length > 0;
  hint.style.display = changed ? 'block' : 'none';
}

function _collectFeishuConfig() {
  var appId = (document.getElementById('settingFeishuAppId') || {}).value || '';
  var secret = (document.getElementById('settingFeishuAppSecret') || {}).value || '';
  var defProj = (document.getElementById('settingFeishuDefaultProject') || {}).value || '';
  var wsRoot = (document.getElementById('settingFeishuWorkspaceRoot') || {}).value || '';
  var au = (document.getElementById('settingFeishuAllowedUsers') || {}).value || '';
  var allowedUsers = au.split('\n').map(function(s) { return s.trim(); }).filter(Boolean);

  var cfg = {
    app_id: appId.trim(),
    default_project: defProj.trim(),
    workspace_root: wsRoot.trim(),
    allowed_users: allowedUsers,
  };
  // Only include secret if user typed something new
  if (secret.trim()) {
    cfg.app_secret = secret.trim();
  }
  return cfg;
}

function _populateAdvancedTab(cfg) {
  _populateResponsesExperimentControls();
  _populateCostExperiment(cfg);
  var pr = document.getElementById('settingPricing');
  if (pr && cfg.pricing) {
    var lines = [];
    for (var model in cfg.pricing) {
      var info = cfg.pricing[model];
      lines.push(model + ': in=$' + info.input + ' out=$' + info.output);
    }
    pr.value = lines.join('\n');
  }
  var si = document.getElementById('settingsServerInfo');
  if (si && cfg.server_info) {
    var html = '';
    for (var k in cfg.server_info) {
      html += '<div class="stg-info-row"><span class="stg-info-label">' + escapeHtml(k) + '</span><span class="stg-info-value">' + escapeHtml(String(cfg.server_info[k])) + '</span></div>';
    }
    si.innerHTML = html;
  }
  /* Populate IndexedDB cache stats */
  _refreshCacheStatsUI();
  if (typeof _renderCredentialsVault === 'function') _renderCredentialsVault();
}

function _setSelectValue(id, value, fallback) {
  var el = /** @type {HTMLSelectElement|null} */ (document.getElementById(id));
  if (!el) return;
  var wanted = String(value || fallback || '');
  var exists = Array.prototype.some.call(el.options || [], function(opt) {
    return opt.value === wanted;
  });
  el.value = exists ? wanted : fallback;
}

function _populateResponsesExperimentControls() {
  var toolsCfg = (config && config.tools) || {};
  var responsesCfg = (config && config.responses) || {};
  var orchestrationCfg = (config && config.orchestration) || {};
  _setSelectValue('settingToolSearch', toolsCfg.toolSearch, 'auto');
  _setSelectValue('settingProgrammaticCalling', toolsCfg.programmaticCalling, 'auto');
  _setSelectValue('settingResponsesPromptProfile', responsesCfg.promptProfile, 'auto');
  _setSelectValue('settingResponsesTransport', responsesCfg.transport, 'sse');
  _setSelectValue('settingResponsesReasoning', responsesCfg.reasoningMode, 'standard');
  _setSelectValue('settingResponsesVerbosity', responsesCfg.verbosity, 'medium');
  _setSelectValue('settingResponsesImageDetail', responsesCfg.imageDetail, 'auto');
  var multiAgentMode = Object.prototype.hasOwnProperty.call(orchestrationCfg, 'multiAgent')
    ? orchestrationCfg.multiAgent : responsesCfg.multiAgent;
  _setSelectValue('settingResponsesMultiAgent', multiAgentMode, 'auto');
  var maxEl = document.getElementById('settingResponsesMaxSubagents');
  var maxAgents = Object.prototype.hasOwnProperty.call(orchestrationCfg, 'maxConcurrentAgents')
    ? orchestrationCfg.maxConcurrentAgents : responsesCfg.maxConcurrentSubagents;
  if (maxEl) maxEl.value = Math.max(1, Math.min(8,
    parseInt(maxAgents, 10) || 3));
  _syncResponsesExperimentUi();
}

function _syncResponsesExperimentUi() {
  // Multi-agent is always on (no 'off' mode), so the concurrency cap is
  // always editable. Kept as a named handler because the select's change
  // action references it.
  var maxEl = document.getElementById('settingResponsesMaxSubagents');
  if (maxEl) maxEl.disabled = false;
}

function _collectResponsesExperimentControls() {
  var value = function(id, fallback) {
    var el = document.getElementById(id);
    return (el && el.value) || fallback;
  };
  var maxEl = document.getElementById('settingResponsesMaxSubagents');
  var maxSubagents = Math.max(1, Math.min(8,
    parseInt(maxEl && maxEl.value, 10) || 3));
  config.tools = Object.assign({}, config.tools || {}, {
    toolSearch: value('settingToolSearch', 'auto'),
    // Composer switches shape model-visible schemas; the browser UI never
    // turns those switches into an execution allow-list. ``selected_only``
    // remains a headless/API compatibility input only.
    executionScope: 'available',
    programmaticCalling: value('settingProgrammaticCalling', 'auto'),
  });
  config.responses = Object.assign({}, config.responses || {}, {
    transport: value('settingResponsesTransport', 'sse'),
    reasoningMode: value('settingResponsesReasoning', 'standard'),
    verbosity: value('settingResponsesVerbosity', 'medium'),
    imageDetail: value('settingResponsesImageDetail', 'auto'),
    promptProfile: value('settingResponsesPromptProfile', 'auto'),
  });
  // Provider-neutral owner. Old saved configs are read above, then migrated
  // on the next save so a Responses-only label can no longer imply that
  // other models lose multi-agent orchestration.
  config.orchestration = Object.assign({}, config.orchestration || {}, {
    multiAgent: value('settingResponsesMultiAgent', 'auto'),
    maxConcurrentAgents: maxSubagents,
  });
  delete config.responses.multiAgent;
  delete config.responses.maxConcurrentSubagents;
}

function _costExperimentInt(id, fallback, min, max) {
  var el = document.getElementById(id);
  var value = parseInt(el && el.value, 10);
  if (!Number.isFinite(value)) value = fallback;
  return Math.max(min, Math.min(max, value));
}

function _populateCostExperiment(cfg) {
  var exp = (cfg && cfg.cost_experiment) || {};
  var enabled = document.getElementById('settingCostExperimentEnabled');
  var id = document.getElementById('settingCostExperimentId');
  var traffic = document.getElementById('settingCostExperimentTraffic');
  var treatment = document.getElementById('settingCostExperimentTreatment');
  var minSample = document.getElementById('settingCostExperimentMinSample');
  if (!enabled) return;
  enabled.checked = exp.enabled === true;
  if (id) id.value = exp.experiment_id || 'context-cost-v1';
  if (traffic) traffic.value = exp.traffic_percent == null ? 10 : exp.traffic_percent;
  if (treatment) treatment.value = exp.treatment_percent == null ? 50 : exp.treatment_percent;
  if (minSample) minSample.value = exp.min_sample_size == null ? 20 : exp.min_sample_size;
  var trafficVal = document.getElementById('costExperimentTrafficVal');
  var treatmentVal = document.getElementById('costExperimentTreatmentVal');
  if (trafficVal) trafficVal.textContent = (traffic ? traffic.value : 10) + '%';
  if (treatmentVal) treatmentVal.textContent = (treatment ? treatment.value : 50) + '%';
  _syncCostExperimentUi();
  _refreshCostExperimentReport();
}

function _syncCostExperimentUi() {
  var enabled = document.getElementById('settingCostExperimentEnabled');
  var isOn = !!(enabled && enabled.checked);
  document.querySelectorAll('.cost-exp-fields input').forEach(function(el) {
    el.disabled = !isOn;
  });
  var controls = document.querySelector('.cost-exp-controls');
  if (controls) controls.classList.toggle('is-off', !isOn);
}

function _collectCostExperimentConfig() {
  var enabled = document.getElementById('settingCostExperimentEnabled');
  if (!enabled) return null;
  var id = document.getElementById('settingCostExperimentId');
  return {
    enabled: !!(enabled && enabled.checked),
    experiment_id: ((id && id.value) || 'context-cost-v1').trim(),
    traffic_percent: _costExperimentInt('settingCostExperimentTraffic', 10, 0, 100),
    treatment_percent: _costExperimentInt('settingCostExperimentTreatment', 50, 1, 99),
    min_sample_size: _costExperimentInt('settingCostExperimentMinSample', 20, 2, 10000),
  };
}

function _costExperimentPct(value) {
  return value == null ? '—' : (Number(value) * 100).toFixed(1) + '%';
}

function _costExperimentMoney(value) {
  return value == null ? '—' : '$' + Number(value).toFixed(4);
}

async function _refreshCostExperimentReport() {
  var el = document.getElementById('costExperimentReport');
  if (!el || typeof Api === 'undefined' || !Api.costExperiments) return;
  el.innerHTML = '<div class="cost-exp-empty">' + escapeHtml(t('settings.costExperimentLoading')) + '</div>';
  try {
    var report = await Api.costExperiments.report(14);
    var arms = (report && report.arms) || {};
    var control = arms.control || {};
    var optimized = arms.optimized || {};
    var decision = (report && report.decision) || {};
    var delta = report && report.comparison && report.comparison.costPerConversationDeltaPct;
    var deltaText = delta == null ? '—' : (delta > 0 ? '+' : '') + Number(delta).toFixed(1) + '%';
    var readiness;
    if (report && report.promotionEligible) {
      readiness = t('settings.costExperimentPromotable');
    } else if (decision.status === 'do_not_promote') {
      readiness = t('settings.costExperimentDoNotPromote');
    } else if (decision.dataValid === false) {
      readiness = t('settings.costExperimentInvalidDecision');
    } else if (report && report.sampleReady) {
      readiness = t('settings.costExperimentGuardrailsPending');
    } else {
      readiness = t('settings.costExperimentCollecting', { n: (report && report.minSampleSize) || 20 });
    }
    var warnings = [];
    if (report && report.truncated) {
      warnings.push(t('settings.costExperimentTruncated', { n: report.rowCap }));
    }
    if (report && report.configurationError) {
      warnings.push(t('settings.costExperimentConfigurationError'));
    }
    var blockers = Array.isArray(decision.blockers) ? decision.blockers : [];
    if (blockers.length) {
      warnings.push(t('settings.costExperimentBlockers', {
        reasons: blockers.map(function(code) {
          var translated = t('settings.costExperimentBlocker.' + code);
          return translated === 'settings.costExperimentBlocker.' + code ? code : translated;
        }).join(' · '),
      }));
    }
    var warning = warnings.map(function(message) {
      return '<div class="cost-exp-warning">' + escapeHtml(message) + '</div>';
    }).join('');
    function armCard(name, arm, cls) {
      return '<div class="cost-exp-result ' + cls + '">' +
        '<strong>' + escapeHtml(name) + '</strong>' +
        '<span>' + escapeHtml(t('settings.costExperimentConversations', { n: arm.conversations || 0 })) + '</span>' +
        '<span>' + escapeHtml(t('settings.costExperimentFullyPriced', { n: arm.fullyPricedAssignmentUnits || 0 })) + '</span>' +
        '<b>' + escapeHtml(_costExperimentMoney(arm.analysisCostPerFullyPricedAssignmentUnitUsd)) + '</b>' +
        '<small>' + escapeHtml(t('settings.costExperimentPerConversation')) + '</small>' +
        '<span>' + escapeHtml(t('settings.costExperimentCoverage')) + ' ' + escapeHtml(_costExperimentPct(arm.assignmentUnitPricingCoverage)) + '</span>' +
        '</div>';
    }
    var digest = String((report && report.specDigest) || '');
    var identity = '';
    if (digest) {
      identity += '<div class="settings-toggle-desc">' + escapeHtml(t('settings.costExperimentSpec', { digest: digest.slice(0, 12) })) + '</div>';
    }
    if (report && report.maximumAssignmentUnits) {
      identity += '<div class="settings-toggle-desc">' + escapeHtml(t('settings.costExperimentHorizon', {
        observed: report.observedAssignmentUnits || 0,
        maximum: report.maximumAssignmentUnits,
      })) + '</div>';
    }
    el.innerHTML = warning + '<div class="cost-exp-summary">' +
      armCard(t('settings.costExperimentArmControl'), control, 'control') +
      '<div class="cost-exp-delta"><span>' + escapeHtml(t('settings.costExperimentDelta')) + '</span><strong>' + escapeHtml(deltaText) + '</strong><small>' + escapeHtml(readiness) + '</small></div>' +
      armCard(t('settings.costExperimentArmOptimized'), optimized, 'optimized') +
      '</div><div class="cost-exp-table-wrap"><table class="cost-exp-table"><thead><tr>' +
      '<th>' + escapeHtml(t('settings.costExperimentMetric')) + '</th><th>' + escapeHtml(t('settings.costExperimentArmControl')) + '</th><th>' + escapeHtml(t('settings.costExperimentArmOptimized')) + '</th>' +
      '</tr></thead><tbody>' +
      '<tr><td>' + escapeHtml(t('settings.costExperimentPromptPerTurn')) + '</td><td>' + escapeHtml(String(control.promptTokensPerTurn == null ? '—' : control.promptTokensPerTurn)) + '</td><td>' + escapeHtml(String(optimized.promptTokensPerTurn == null ? '—' : optimized.promptTokensPerTurn)) + '</td></tr>' +
      '<tr><td>' + escapeHtml(t('settings.costExperimentCacheReadRatio')) + '</td><td>' + escapeHtml(_costExperimentPct(control.cacheReadRatio)) + '</td><td>' + escapeHtml(_costExperimentPct(optimized.cacheReadRatio)) + '</td></tr>' +
      '<tr><td>' + escapeHtml(t('settings.costExperimentLatencyAvg')) + '</td><td>' + escapeHtml(control.latencyAvgMs == null ? '—' : Math.round(control.latencyAvgMs) + ' ms') + '</td><td>' + escapeHtml(optimized.latencyAvgMs == null ? '—' : Math.round(optimized.latencyAvgMs) + ' ms') + '</td></tr>' +
      '<tr><td>' + escapeHtml(t('settings.costExperimentLatencyP90')) + '</td><td>' + escapeHtml(control.latencyP90Ms == null ? '—' : Math.round(control.latencyP90Ms) + ' ms') + '</td><td>' + escapeHtml(optimized.latencyP90Ms == null ? '—' : Math.round(optimized.latencyP90Ms) + ' ms') + '</td></tr>' +
      '<tr><td>' + escapeHtml(t('settings.costExperimentOraclePass')) + '</td><td>' + escapeHtml(_costExperimentPct(control.oraclePassRate)) + '</td><td>' + escapeHtml(_costExperimentPct(optimized.oraclePassRate)) + '</td></tr>' +
      '<tr><td>' + escapeHtml(t('settings.costExperimentErrorFree')) + '</td><td>' + escapeHtml(_costExperimentPct(control.terminalWithoutErrorRate)) + '</td><td>' + escapeHtml(_costExperimentPct(optimized.terminalWithoutErrorRate)) + '</td></tr>' +
      '</tbody></table></div>' + identity;
  } catch (e) {
    el.innerHTML = '<div class="cost-exp-warning">' + escapeHtml(t('settings.costExperimentLoadFailed')) + ': ' + escapeHtml(e && e.message || '') + '</div>';
  }
}

/** Refresh the cache statistics display in Settings > Advanced */
function _refreshCacheStatsUI() {
  var el = document.getElementById('settingsCacheStats');
  if (!el) return;
  if (typeof ConvCache === 'undefined' || !ConvCache.isAvailable()) {
    el.textContent = t('settings.cacheUnavailable');
    return;
  }
  ConvCache.stats().then(function(s) {
    el.textContent = t('settings.cacheCached', { n: s.count });
  });
}

/** Handler for the "Clear Cache" button in settings */
function _clearConvCacheFromSettings() {
  if (typeof ConvCache === 'undefined') return;
  var btn = document.getElementById('settingsClearCacheBtn');
  if (btn) { btn.disabled = true; btn.textContent = t('settings.cacheClearing'); }
  ConvCache.clear().then(function() {
    _refreshCacheStatsUI();
    if (btn) { btn.disabled = false; btn.innerHTML = Icon('trash', 12) + ' ' + t('settings.cacheClearBtn'); }
    // Force all in-memory conversations to _turnSnapshotRequired so next click refetches
    runtimeScope.conversations.forEach(function(c) {
      if (c.id !== runtimeScope.activeConvId) c._turnSnapshotRequired = true;
    });
    if (typeof showToast === 'function') showToast(t('settings.cacheCleared'));
  });
}
/* ===== migrated source: settings/oauth.js ===== */
/* ═══════════════════════════════════════════════════════════════════
   settings/oauth — extracted from settings.js (split 2026-05-28)

   OAuth flows: status/login/logout/manual-callback for Claude/Codex providers.

   This file is concatenated by Vite's module graph — symbols share
   the same window scope as every other frontend/src/runtime/*.js file. No
   exports / imports needed.
   ═══════════════════════════════════════════════════════════════════ */

// ══════════════════════════════════════════════════════
//  OAuth Subscription Login — Browser-Centric Flow
//
//  Flow:
//    1. User clicks "登录" → fetch /api/oauth/login → get auth_url
//    2. Open auth_url in popup window (window.open)
//    3. User authenticates in popup
//    4. OAuth redirect → local relay when browser/server are colocated;
//       remote Codex deployments copy the complete localhost callback URL
//    5. Relay page postMessages the code, or the user pastes that URL
//    6. We receive the code via 'message' event listener
//    7. Send code to /api/oauth/callback → server exchanges for tokens
//
//  All browser-driven. Server only does: PKCE generation + token exchange.
// ══════════════════════════════════════════════════════

// ── Pending-flow registry + callback message gate ──
// ANY window can postMessage us and any same-origin page can broadcast, so a
// bare `type: 'oauth_callback'` check accepts injected codes/states from
// unrelated pages. The relay page is served by OUR loopback relay on the
// flow's callback port and echoes the flow's server-minted state, so a
// legitimate callback is provable on two axes: sender origin and per-flow
// state nonce. Both are recorded here when a login starts.
var _oauthPendingFlows = {};

var _OAUTH_RELAY_DEFAULT_PORTS = { claude: 54545, codex: 1455 };

function _oauthRelayOrigins(provider, port) {
  var p = Number(port) || _OAUTH_RELAY_DEFAULT_PORTS[provider] || 0;
  if (!p) return [];
  // The relay binds 127.0.0.1 but the registered redirect may say
  // `localhost` — the popup's final origin can be either spelling.
  return ['http://127.0.0.1:' + p, 'http://localhost:' + p];
}

function _oauthRecordPendingFlow(provider, port, state) {
  _oauthPendingFlows[provider] = {
    state: state || '',
    origins: _oauthRelayOrigins(provider, port),
  };
}

function _oauthClearPendingFlow(provider) {
  delete _oauthPendingFlows[provider];
}

// origin === null marks the BroadcastChannel path: it is same-origin by
// construction, so there is no sender origin to verify and the pending-flow
// state check is the whole gate.
function _oauthCallbackMessageAllowed(provider, state, origin) {
  var pending = provider && _oauthPendingFlows[provider];
  if (!pending) {
    console.warn('[OAuth] Ignoring callback for %s — no pending flow', provider);
    return false;
  }
  if (origin !== null && pending.origins.length &&
      pending.origins.indexOf(origin) < 0) {
    console.warn('[OAuth] Rejecting %s callback from unexpected origin: %s',
      provider, origin);
    return false;
  }
  if (pending.state && state !== pending.state) {
    console.warn('[OAuth] Rejecting %s callback — state mismatch', provider);
    return false;
  }
  return true;
}

// ── Global postMessage listener for OAuth callbacks ──
// The relay page (served by the server's lightweight HTTP relay) sends
// the authorization code back to us via postMessage or BroadcastChannel.
(function _initOAuthMessageListener() {
  // postMessage from popup's relay page
  window.addEventListener('message', function(event) {
    var data = event.data;
    if (!data || data.type !== 'oauth_callback') return;
    if (!_oauthCallbackMessageAllowed(data.provider, data.state, event.origin || '')) return;
    console.log('[OAuth] Received code via postMessage from relay page for:', data.provider);
    _handleOAuthCode(data.provider, data.code, data.state);
  });

  // BroadcastChannel fallback (works even if popup loses window.opener ref)
  try {
    var bc = new BroadcastChannel('oauth_callback');
    bc.onmessage = function(event) {
      var data = event.data;
      if (!data || data.type !== 'oauth_callback') return;
      if (!_oauthCallbackMessageAllowed(data.provider, data.state, null)) return;
      console.log('[OAuth] Received code via BroadcastChannel for:', data.provider);
      _handleOAuthCode(data.provider, data.code, data.state);
    };
  } catch(e) {
    // BroadcastChannel not supported — postMessage still works
  }
})();

// Browser-side exchange params per provider, captured from the login response.
var _oauthExchangeParams = {};

// ── Browser-side token exchange (B1 geo-block workaround) ──
// Exchanges the auth code against the provider's token endpoint FROM THE
// BROWSER (using the user's VPN/proxy), then hands the resulting token to
// the server to persist. Returns a Promise that resolves to the parsed
// token JSON on success, or rejects (so the caller falls back to the
// server-side exchange). Anthropic/OpenAI token endpoints are CORS-open for
// the public OAuth client, but if not, the fetch rejects and we fall back.
function _browserExchange(provider, code, state) {
  var ex = _oauthExchangeParams[provider];
  if (!ex || !ex.token_url || !ex.code_verifier) return Promise.reject(new Error('no-exchange-params'));

  var headers, bodyData;
  if (ex.style === 'form') {
    headers = { 'Content-Type': 'application/x-www-form-urlencoded' };
    var p = new URLSearchParams();
    p.set('grant_type', 'authorization_code');
    p.set('code', code);
    p.set('redirect_uri', ex.redirect_uri);
    p.set('client_id', ex.client_id);
    p.set('code_verifier', ex.code_verifier);
    bodyData = p.toString();
  } else {
    headers = { 'Content-Type': 'application/json' };
    bodyData = JSON.stringify({
      grant_type: 'authorization_code',
      code: code,
      state: state || ex.state || '',
      redirect_uri: ex.redirect_uri,
      client_id: ex.client_id,
      code_verifier: ex.code_verifier,
    });
  }

  // Direct cross-origin fetch to the provider token endpoint, via the
  // browser's own network. No credentials — this is a public OAuth client.
  return fetch(ex.token_url, { method: 'POST', headers: headers, body: bodyData, mode: 'cors' })
    .then(function(r) {
      return r.text().then(function(txt) {
        var json; try { json = JSON.parse(txt); } catch (e) { json = null; }
        if (!r.ok || !json || !json.access_token) {
          var msg = (json && (json.error_description || (json.error && json.error.message) || json.error)) || ('HTTP ' + r.status);
          var err = new Error('exchange-failed: ' + msg);
          err._upstreamStatus = r.status;
          throw err;
        }
        return json;
      });
    });
}

// Persist a browser-exchanged token via the server. Returns the parsed
// JSON result (with .error on failure).
function _storeBrowserToken(provider, tokenJson) {
  return Api.oauth.storeToken(provider, tokenJson)
    .then(function(r) { return r.json(); });
}

// ── Server-side token exchange (primary path, S2) ──
// POSTs the raw code to /api/oauth/callback so the SERVER does the exchange.
// The server auto-routes direct OR through an egress-capable desktop agent,
// so this path works even when the server's own egress is geo-blocked.
// Rejection Error carries `_statusCode` from the server's error body
// (403 geo-block / 0 network-or-egress-unavailable / 400-401 auth rejection)
// so _completeLogin can classify whether a browser retry makes sense.
function _serverExchange(provider, code, state, manual) {
  var body = { provider: provider, code: code };
  if (state) body.state = state;
  if (manual) body.manual = true;
  function _req(useGet) {
    if (useGet) {
      var qs = 'provider=' + encodeURIComponent(provider) + '&code=' + encodeURIComponent(code);
      if (state) qs += '&state=' + encodeURIComponent(state);
      if (manual) qs += '&manual=1';
      return Api.oauth.callbackGet(qs);
    }
    return Api.oauth.callbackPost(body);
  }
  return _req(false)
    .then(function(r) { return (r.status === 404 || r.status === 405) ? _req(true) : r; })
    .then(function(r) {
      if (!r.ok) return r.text().then(function(t) {
        var j; try { j = JSON.parse(t); } catch (e) { j = null; }
        var err = new Error((j && j.error) || t.slice(0, 200));
        if (j && typeof j.status_code !== 'undefined') err._statusCode = j.status_code;
        throw err;
      });
      return r.json();
    });
}

// ── Complete a login given an auth code: server → browser recovery ──
// Tofu owns transport selection and recovery; the user only authorizes.
// Order:
// 1. Server exchange — auto-routes direct OR through an egress-capable
//    desktop agent (S2), so it now works even when the server's own egress
//    is geo-blocked, and has no CORS exposure. A genuine auth rejection
//    (400/401: code expired/used) is surfaced as-is — the code is burned,
//    retrying it anywhere else just fails again.
// 2. Browser exchange (B1) — only when the server failed with a geo-block
//    (403) / network error / egress-unavailable (status_code 0), i.e. the
//    code is provably still unconsumed.
function _completeLogin(provider, code, state, opts) {
  // manual: the user pasted the code/URL by hand — the only path allowed to
  // arrive without the flow's state (raw code paste has no state channel).
  var manual = !!(opts && opts.manual);
  var capProvider = provider === 'codex' ? 'Codex' : 'Claude';
  _updateOAuthCard(provider, { status: 'exchanging' });

  function _onSuccess(data) {
    _oauthClearPendingFlow(provider);
    // Exchange/store responses historically returned `{ok, email}` without
    // the status projection fields consumed by _updateOAuthCard. Passing that
    // object through made a successful login repaint as "not logged in".
    // Normalize the success fact at this boundary while preserving richer
    // provider/model metadata when the backend supplies it.
    var success = Object.assign({}, data || {}, {
      status: 'success', authenticated: true,
    });
    _updateOAuthCard(provider, success);
    _autoConfigureOAuthProvider(provider, success);
    var manualDiv = document.getElementById('oauth' + capProvider + 'Manual');
    if (manualDiv) manualDiv.style.display = 'none';
    var manualInput = document.getElementById('oauth' + capProvider + 'ManualUrl');
    if (manualInput) manualInput.value = '';
  }
  function _onError(msg) {
    _oauthClearPendingFlow(provider);
    _updateOAuthCard(provider, { status: 'error' });
    showAlert(t('settings.oauthAuthorizationFailed', { msg: msg }));
  }

  function _recoveryFailed(reason) {
    _oauthClearPendingFlow(provider);
    console.error('[OAuth] Automatic recovery exhausted for %s: %s', provider, reason || 'unknown');
    _updateOAuthCard(provider, { status: 'error' });
    showAlert(t('settings.oauthAutomaticRecoveryFailed'));
  }

  function _tryBrowser(reason) {
    console.warn('[OAuth] Server exchange unavailable (%s) — trying browser exchange', reason);
    _browserExchange(provider, code, state)
      .then(function(tokenJson) {
        console.log('[OAuth] Browser-side exchange succeeded for', provider);
        return _storeBrowserToken(provider, tokenJson).then(function(data) {
          if (!data || data.error) {
            _recoveryFailed((data && data.error) || 'store failed');
            return;
          }
          _onSuccess(data);
        });
      })
      .catch(function(e2) { _recoveryFailed((e2 && e2.message) || 'browser exchange failed'); });
  }

  _serverExchange(provider, code, state, manual)
    .then(function(data) {
      if (!data || data.error) { _tryBrowser((data && data.error) || 'empty result'); return; }
      _onSuccess(data);
    })
    .catch(function(e) {
      var sc = e && e._statusCode;
      if (sc === 400 || sc === 401) {
        // Genuine auth rejection — the code is consumed/expired; don't burn
        // it a second time from the browser.
        _onError(e.message);
        return;
      }
      // 403 geo-block / 0 network-or-egress-unavailable / unknown — the code
      // was rejected at the edge BEFORE grant processing, so it is still
      // redeemable from the browser's own network.
      _tryBrowser(e.message);
    });
}

// ── Handle received OAuth code (from postMessage / relay) ──
function _handleOAuthCode(provider, code, state) {
  if (!provider || !code) return;
  _completeLogin(provider, code, state);
}

function _loadOAuthStatus(fromRepoll) {
  // Typed owner; remove this bridge when the subscription presenter migrates.
  renderCodexSharedLogin(undefined, function(result) {
    _updateOAuthCard('codex', result);
    _autoConfigureOAuthProvider('codex', result);
  });
  if (!fromRepoll) {
    _oauthStatusRepollAttempts = 0;
  }
  Api.oauth.status()
    .then(function(data) {
      if (!data) return;
      _updateOAuthCard('claude', data.claude);
      _updateOAuthCard('codex', data.codex);
      _catpawLoadStatus();
      // Egress and earned-reset reads both warm asynchronously. One bounded
      // status re-poll chain observes either without duplicating timers.
      var probing = [data.claude, data.codex].some(function(s) {
        return s && s.egress && s.egress.state === 'unknown';
      });
      var resetRefreshing = !!(data.codex && data.codex.reset_offer &&
        data.codex.reset_offer.refreshing);
      if (resetRefreshing) _scheduleOAuthStatusRepoll();
      if (!probing && !resetRefreshing) _oauthStatusRepollAttempts = 0;
    })
    .catch(function(e) {
      console.warn('[OAuth] Failed to load status:', e);
    });
}

function _oauthQuotaPct(value) {
  var n = Number(value);
  if (!Number.isFinite(n)) return '';
  return (Math.round(n * 10) / 10).toFixed(1).replace(/\.0$/, '');
}

function _oauthQuotaWindowLabel(minutes) {
  var n = Number(minutes || 0);
  if (n === 300) return t('quota.window5h');
  if (n === 10080) return t('quota.window7d');
  if (n > 0 && n % 1440 === 0) return t('quota.windowDays', { n: n / 1440 });
  if (n > 0 && n % 60 === 0) return t('quota.windowHours', { n: n / 60 });
  if (n > 0) return t('quota.windowMinutes', { n: n });
  return t('quota.windowUnknown');
}

function _oauthQuotaResetLabel(timestamp) {
  var seconds = Number(timestamp || 0);
  if (!Number.isFinite(seconds) || seconds <= 0) return '';
  var when = new Date(seconds * 1000);
  if (!Number.isFinite(when.getTime())) return '';
  try {
    var locale = (typeof _i18nLang !== 'undefined' && _i18nLang === 'zh')
      ? 'zh-CN' : 'en-US';
    return new Intl.DateTimeFormat(locale, {
      month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit',
    }).format(when);
  } catch (_err) {
    return '';
  }
}

function _oauthQuotaAgeLabel(ageSeconds) {
  var s = Number(ageSeconds);
  if (!Number.isFinite(s) || s < 0) return '';
  if (s < 60) return t('settings.oauthQuotaAgeJust');
  if (s < 3600) return t('settings.oauthQuotaAgeMinutes', { n: Math.floor(s / 60) });
  return t('settings.oauthQuotaAgeHours', { n: Math.floor(s / 3600) });
}

function _renderOAuthQuota(provider, quota, authenticated, refreshing) {
  var capProvider = provider === 'codex' ? 'Codex' : 'Claude';
  var el = document.getElementById('oauth' + capProvider + 'Quota');
  if (!el) return;
  if (provider !== 'codex' || !authenticated) {
    el.style.display = 'none';
    el.innerHTML = '';
    return;
  }
  el.style.display = '';
  if (!quota || (!quota.primary && !quota.secondary)) {
    el.innerHTML = '<div class="oauth-quota-title">' +
      escapeHtml(t('settings.oauthQuotaTitle')) + '</div>' +
      '<div class="oauth-quota-pending">' +
      escapeHtml(t(refreshing
        ? 'settings.oauthQuotaChecking'
        : 'settings.oauthQuotaPending')) + '</div>';
    return;
  }
  var rows = [];
  ['primary', 'secondary'].forEach(function(name) {
    var win = quota[name];
    if (!win || !Number.isFinite(Number(win.remaining_percent))) return;
    var remaining = Math.max(0, Math.min(100, Number(win.remaining_percent)));
    var label = _oauthQuotaWindowLabel(win.window_minutes);
    var resetTime = _oauthQuotaResetLabel(win.resets_at);
    var resetCopy = resetTime
      ? '<span class="oauth-quota-reset">' + escapeHtml(t(
        'settings.oauthQuotaResetsAt', { time: resetTime })) + '</span>'
      : '';
    rows.push('<div class="oauth-quota-row">' +
      '<div class="oauth-quota-row-head"><span class="oauth-quota-window">' +
      escapeHtml(label) + resetCopy + '</span>' +
      '<span>' + escapeHtml(t('settings.oauthQuotaRemaining', {
        remaining: _oauthQuotaPct(remaining) })) + '</span></div>' +
      '<div class="oauth-quota-track"><span style="width:' + remaining + '%"></span></div>' +
      '</div>');
  });
  var sourceLabel = t(quota.source === 'usage_api'
    ? 'settings.oauthQuotaSourceLive'
    : 'settings.oauthQuotaSource');
  var ageLabel = _oauthQuotaAgeLabel(quota.age_seconds);
  el.innerHTML = '<div class="oauth-quota-title">' +
    escapeHtml(t('settings.oauthQuotaTitle')) + '</div>' + rows.join('') +
    '<div class="oauth-quota-source">' +
    escapeHtml(ageLabel ? sourceLabel + ' · ' + ageLabel : sourceLabel) +
    '</div>';
}

function _oauthResetExpiryLabel(timestamp) {
  var seconds = Number(timestamp || 0);
  if (!Number.isFinite(seconds) || seconds <= 0) return '';
  try {
    var locale = (typeof _i18nLang !== 'undefined' && _i18nLang === 'zh')
      ? 'zh-CN' : 'en';
    return new Date(seconds * 1000).toLocaleString(locale, {
      dateStyle: 'medium', timeStyle: 'short',
    });
  } catch (_err) {
    return '';
  }
}

function _renderOAuthResetOffer(provider, offer, authenticated) {
  var capProvider = provider === 'codex' ? 'Codex' : 'Claude';
  var el = document.getElementById('oauth' + capProvider + 'ResetOffer');
  if (!el) return;
  el.style.display = 'none';
  el.className = 'oauth-reset-offer';
  el.innerHTML = '';
  if (provider !== 'codex' || !authenticated || !offer) return;

  if (offer.state === 'unknown' && offer.refreshing) {
    el.className += ' is-checking';
    el.innerHTML = '<div class="oauth-reset-offer-title">' +
      escapeHtml(t('settings.oauthResetAvailableTitle')) + '</div>' +
      '<div class="oauth-reset-offer-copy">' +
      escapeHtml(t('settings.oauthResetChecking')) + '</div>';
    el.style.display = '';
    return;
  }
  var count = Number(offer.available_count || 0);
  if (offer.state !== 'available' || !Number.isInteger(count) || count <= 0) return;

  var detail = count === 1
    ? t('settings.oauthResetAvailableOne')
    : t('settings.oauthResetAvailableMany', { count: count });
  var meta = [];
  var expiry = _oauthResetExpiryLabel(offer.expires_at);
  if (expiry) meta.push(t('settings.oauthResetExpires', { time: expiry }));
  if (offer.stale) meta.push(t('settings.oauthResetStale'));
  el.className += offer.stale ? ' is-stale' : ' is-available';
  el.innerHTML = '<div class="oauth-reset-offer-title">' +
    escapeHtml(t('settings.oauthResetAvailableTitle')) + '</div>' +
    '<div class="oauth-reset-offer-copy">' + escapeHtml(detail) + '</div>' +
    (meta.length ? '<div class="oauth-reset-offer-meta">' +
      escapeHtml(meta.join(' · ')) + '</div>' : '') +
    (offer.stale ? '' : '<div class="oauth-reset-offer-actions">' +
      '<button type="button" class="btn-small btn-primary" ' +
      'data-tofu-action="_oauthConsumeReset()">' +
      escapeHtml(t('settings.oauthResetRedeemButton')) + '</button></div>') +
    '<div class="oauth-reset-offer-hint">' +
      escapeHtml(t('settings.oauthResetRedeemHint')) + '</div>';
  el.style.display = '';
}

// ── Asynchronous OAuth-status re-poll ──
// Egress reachability and Codex reset-credit detection both warm off-request.
// The first status read therefore may be `unknown+refreshing`. Re-poll only
// while Settings is open, with one timer and a hard attempt cap; ordinary
// steady-state reads pay no polling cost.
var _oauthStatusRepollTimer = null;
var _oauthStatusRepollAttempts = 0;
var _OAUTH_STATUS_REPOLL_MS = 2000;
var _OAUTH_STATUS_REPOLL_MAX = 8;  // usage + optional details are each <= 5s

function _scheduleOAuthStatusRepoll() {
  if (_oauthStatusRepollTimer) return;
  if (_oauthStatusRepollAttempts >= _OAUTH_STATUS_REPOLL_MAX) return;
  _oauthStatusRepollAttempts++;
  _oauthStatusRepollTimer = setTimeout(function() {
    _oauthStatusRepollTimer = null;
    var modal = document.getElementById('settingsModal');
    if (!modal || !modal.classList.contains('open')) {
      _oauthStatusRepollAttempts = 0;
      return;
    }
    _loadOAuthStatus(true);
  }, _OAUTH_STATUS_REPOLL_MS);
}

// ── Desktop-egress status line + pin selector (S4) ──
// Renders the server-computed egress state per card. NEVER probes inline —
// the server's status payload carries a cached verdict only.
function _renderEgressLine(provider, egress) {
  var capProvider = provider === 'codex' ? 'Codex' : 'Claude';
  var el = document.getElementById('oauth' + capProvider + 'Egress');
  if (!el) return;
  el.style.display = 'none';
  el.className = 'oauth-egress-line';
  el.textContent = '';
  el.innerHTML = '';
  if (!egress || !egress.state) return;

  var target = provider === 'codex' ? 'OpenAI' : 'Anthropic';
  var key = '';
  var vars = { provider: target };
  var visual = 'is-warning';
  if (egress.state === 'unknown') {
    key = 'settings.egressChecking';
    visual = 'is-checking';
    _scheduleOAuthStatusRepoll();
  } else if (egress.state === 'direct') {
    var routeId = egress.preferred_server_route || '';
    var routeMode = egress.preferred_server_route_mode ||
      ((routeId === 'env' || routeId.indexOf('pool:') === 0) ? 'proxy' : 'direct');
    if (routeMode === 'proxy' || routeMode === 'env') {
      key = 'settings.egressViaProxy';
      vars.route = egress.preferred_server_route_label || routeId ||
        t('settings.egressConfiguredProxy');
    } else {
      key = 'settings.egressDirect';
    }
    visual = 'is-ok';
  } else if (egress.state === 'agent') {
    key = 'settings.egressViaAgent';
    var agent = (egress.agents || [])[0] || {};
    vars.agent = agent.name || agent.agent_id || t('settings.egressDesktopAgent');
    visual = 'is-ok';
  } else if (egress.state === 'agent_no_capability') {
    key = 'settings.egressAgentNoCap';
  } else {
    key = 'settings.egressUnavailable';
    visual = 'is-error';
  }
  el.className = 'oauth-egress-line ' + visual;
  el.textContent = t(key, vars);
  el.style.display = '';
}

function _updateOAuthCard(provider, status) {
  if (!status) return;
  if (provider === 'codex') renderCodexSharedLogin(status);
  var capProvider = provider === 'codex' ? 'Codex' : 'Claude';
  _renderEgressLine(provider, status.egress);
  var badge = document.getElementById('oauth' + capProvider + 'Status');
  var info = document.getElementById('oauth' + capProvider + 'Info');
  var email = document.getElementById('oauth' + capProvider + 'Email');
  var loginBtn = document.getElementById('oauth' + capProvider + 'LoginBtn');
  var logoutBtn = document.getElementById('oauth' + capProvider + 'LogoutBtn');

  if (!badge) return;
  badge.title = '';
  _renderOAuthQuota(provider, status.quota, Boolean(status.authenticated),
    !!(status.reset_offer && status.reset_offer.refreshing));
  _renderOAuthResetOffer(
    provider, status.reset_offer, Boolean(status.authenticated));

  // Device-authorization flow (codex): show the code panel while a device
  // flow waits, hide it in every terminal state, and keep the entry button
  // in sync with the login button's visibility rules.
  if (provider === 'codex') {
    var deviceBtn = document.getElementById('oauthCodexDeviceBtn');
    var devWaiting = !status.authenticated &&
      (status.status === 'started' || status.status === 'waiting_callback');
    if (devWaiting && status.device) {
      _showDevicePanel(status.device.user_code, status.device.verification_url);
    } else if (status.authenticated || !devWaiting) {
      _hideDevicePanel();
      _stopDeviceStatusPoll();
    }
    if (deviceBtn) {
      deviceBtn.style.display = status.authenticated ? 'none' : '';
      if (!status.authenticated && !devWaiting) {
        deviceBtn.disabled = false;
        deviceBtn.textContent = t('settings.oauthDeviceLogin');
      }
    }
  }

  if (status.authenticated) {
    var ready = status.provider_ready !== false;
    badge.textContent = ready
      ? t('settings.oauthModelsReady', { n: status.model_count || 0 })
      : t('settings.oauthAutomaticRecovery');
    badge.className = 'oauth-status-badge ' + (ready ? 'authenticated' : 'pending');
    if (info) { info.style.display = ''; }
    if (email) {
      email.textContent = (status.email || t('settings.oauthConnectedAccount')) + (ready ? '' :
        ' · ' + t('settings.oauthProviderRepairing'));
    }
    if (loginBtn) { loginBtn.style.display = 'none'; }
    if (logoutBtn) { logoutBtn.style.display = ''; }
  } else if (status.status === 'started' || status.status === 'waiting_callback' || status.status === 'exchanging') {
    badge.textContent = status.status === 'exchanging' ? t('settings.oauthGettingToken') : t('settings.oauthWaitingAuth');
    badge.className = 'oauth-status-badge pending';
    if (info) { info.style.display = 'none'; }
    // Show a cancel/retry button so users aren't stuck forever
    if (loginBtn) {
      loginBtn.disabled = false;
      loginBtn.textContent = t('settings.oauthCancelRetry');
      loginBtn.onclick = function() { _oauthCancelAndRetry(provider); };
    }
    if (logoutBtn) { logoutBtn.style.display = 'none'; }
    // A page reload mid-flow lands HERE — not in _oauthLogin's callback —
    // re-rendered from the status projection alone. Restore the manual box,
    // its truthful instructions, and the escape hatch from that projection,
    // or the reloaded page silently offers nothing but a retry that re-runs
    // the same (possibly broken) callback decision — the exact loop the
    // hatch exists to break. Synthetic waiting states (exchange in flight,
    // curl helper) carry no redirect_mode and are left untouched.
    if (status.redirect_mode && status.redirect_mode !== 'device' &&
        status.status !== 'exchanging') {
      var flowManual = document.getElementById('oauth' + capProvider + 'Manual');
      if (flowManual) {
        flowManual.style.display = '';
        var flowUrl = document.getElementById('oauth' + capProvider + 'AuthUrl');
        if (flowUrl && status.auth_url) flowUrl.value = status.auth_url;
      }
      _oauthApplyRedirectMode(provider, status.redirect_mode);
    }
    // Restore browser-recovery parameters after a reload so Tofu can keep
    // handling the exchange without asking the user for infrastructure work.
    if (status.exchange) {
      _oauthExchangeParams[provider] = status.exchange;
      // Re-arm the callback gate too: the login response is gone after a
      // reload, but the status projection still carries the flow's state
      // nonce (the port falls back to the provider's registered default).
      if (!_oauthPendingFlows[provider]) {
        _oauthRecordPendingFlow(provider, 0, status.exchange.state || '');
      }
    }
  } else if (status.status === 'error') {
    badge.textContent = t('settings.oauthError');
    badge.className = 'oauth-status-badge error';
    badge.title = errorEnvelopeMessage(status.error);
    if (info) { info.style.display = 'none'; }
    if (loginBtn) { loginBtn.disabled = false; loginBtn.textContent = provider === 'codex' ? t('settings.oauthLoginChatGPT') : t('settings.oauthLoginClaude'); loginBtn.onclick = function() { _oauthLogin(provider); }; }
    if (logoutBtn) { logoutBtn.style.display = 'none'; }
  } else {
    badge.textContent = t('settings.oauthNotLoggedIn');
    badge.className = 'oauth-status-badge';
    if (info) { info.style.display = 'none'; }
    if (loginBtn) { loginBtn.disabled = false; loginBtn.textContent = provider === 'codex' ? t('settings.oauthLoginChatGPT') : t('settings.oauthLoginClaude'); loginBtn.style.display = ''; loginBtn.onclick = function() { _oauthLogin(provider); }; }
    if (logoutBtn) { logoutBtn.style.display = 'none'; }
  }
}

function _oauthCancelAndRetry(provider) {
  var capProvider = provider === 'codex' ? 'Codex' : 'Claude';
  _oauthClearPendingFlow(provider);
  // Call logout to reset the server-side flow state
  Api.oauth.logoutPost(provider).catch(function() {});
  // Reset UI immediately
  _updateOAuthCard(provider, { status: 'not_started', authenticated: false });
  // Restore normal onclick
  var loginBtn = document.getElementById('oauth' + capProvider + 'LoginBtn');
  if (loginBtn) {
    loginBtn.onclick = function() { _oauthLogin(provider); };
  }
  // Hide manual paste box
  var manualDiv = document.getElementById('oauth' + capProvider + 'Manual');
  if (manualDiv) manualDiv.style.display = 'none';
  // Hide device panel + stop its status poll (codex)
  if (provider === 'codex') {
    _stopDeviceStatusPoll();
    _hideDevicePanel();
    var deviceBtn = document.getElementById('oauthCodexDeviceBtn');
    if (deviceBtn) {
      deviceBtn.disabled = false;
      deviceBtn.textContent = t('settings.oauthDeviceLogin');
    }
  }
}

// ── Which callback is this flow actually walking, and how to get out ──
// Whether Anthropic accepts the loopback redirect for our client is an
// EXTERNAL fact we cannot verify locally. If it ever refuses, a desktop user
// lands on an authorization error with NOTHING to paste (the console page is
// what renders the code, and a loopback flow never reaches it) — and the
// cancel/retry button re-runs the SAME decision, so the user would loop
// through the identical broken flow forever. The way out therefore has to be
// a first-class control in the product, not the TOFU_OAUTH_LOOPBACK env var:
// a packaged .exe user has nowhere to set one.
function _oauthApplyRedirectMode(provider, mode) {
  if (provider !== 'claude') return;   // codex has exactly one registered redirect
  var loopback = mode === 'loopback';
  var pasteHint = document.getElementById('oauthClaudeCodeHint');
  var pasteRow = document.getElementById('oauthClaudePasteRow');
  var lbNote = document.getElementById('oauthClaudeLoopbackNote');
  var fbRow = document.getElementById('oauthClaudeConsoleFallbackRow');
  // The paste instructions are only TRUE on the console flow.
  if (pasteHint) pasteHint.style.display = loopback ? 'none' : '';
  if (pasteRow) pasteRow.style.display = loopback ? 'none' : '';
  // The note + escape hatch are only MEANINGFUL on the loopback flow.
  if (lbNote) lbNote.style.display = loopback ? '' : 'none';
  if (fbRow) fbRow.style.display = loopback ? '' : 'none';
  var btn = document.getElementById('oauthClaudeConsoleFallbackBtn');
  if (btn) btn.onclick = function() { _oauthUseConsoleFallback('claude'); };
}

// Restart the flow pinned to the console callback (manual code paste).
// A fresh flow is required rather than reusing the pending one: the
// redirect_uri is baked into the authorize URL AND must be echoed at
// exchange time, so the old flow's PKCE/state pair cannot be reused with a
// different redirect.
function _oauthUseConsoleFallback(provider) {
  var capP = provider === 'codex' ? 'Codex' : 'Claude';
  // Drop the pending flow so its relay releases the port and its state is
  // not mistaken for the new one.
  Api.oauth.logoutPost(provider).catch(function() {});
  var input = document.getElementById('oauth' + capP + 'ManualUrl');
  if (input) input.value = '';
  _oauthLogin(provider, true);
}

function _oauthLogin(provider, preferConsole) {
  var capProvider = provider === 'codex' ? 'Codex' : 'Claude';
  var loginBtn = document.getElementById('oauth' + capProvider + 'LoginBtn');
  if (loginBtn && loginBtn.disabled) return;
  if (loginBtn) { loginBtn.disabled = true; loginBtn.textContent = t('settings.oauthPreparing'); }

  // Step 1: Ask server to generate PKCE + auth URL + start relay server
  // Try POST first; if proxy returns 404/405, fall back to GET with query params
  // (VSCode tunnel proxies may not forward POST to unknown paths)
  function _doLoginRequest(useGet) {
    if (useGet) {
      console.warn('[OAuth] POST failed, retrying as GET for /api/oauth/login');
      return Api.oauth.loginGet(provider, preferConsole);
    }
    return Api.oauth.loginPost(provider, preferConsole);
  }
  _doLoginRequest(false)
    .then(function(r) {
      if (r.status === 404 || r.status === 405) return _doLoginRequest(true);
      return r;
    })
    .then(function(r) {
      if (!r.ok) {
        return r.text().then(function(t) { throw new Error('HTTP ' + r.status + ': ' + t.slice(0, 200)); });
      }
      return r.json();
    })
    .then(function(data) {
      if (data.error) {
        showAlert(t('settings.oauthLoginFailed', {
          error: errorEnvelopeMessage(data.error) || String(data.error),
        }));
        if (loginBtn) { loginBtn.disabled = false; loginBtn.textContent = provider === 'codex' ? t('settings.oauthLoginChatGPT') : t('settings.oauthLoginClaude'); }
        return;
      }

      if (data.status === 'success') {
        _oauthClearPendingFlow(provider);
        var reused = Object.assign({}, data, { authenticated: true });
        _updateOAuthCard(provider, reused);
        _autoConfigureOAuthProvider(provider, reused);
        return;
      }

      // Stash browser-side exchange params (B1): when the server's egress is
      // geo-blocked from the provider token endpoint, the browser (with the
      // user's VPN) does the exchange itself. code_verifier is OUR PKCE
      // secret, so it's fine to keep it client-side for the duration.
      _oauthExchangeParams[provider] = data.exchange || null;
      // Arm the callback gate for THIS flow before the popup can navigate
      // back: only our relay origin echoing this flow's state gets through.
      _oauthRecordPendingFlow(
        provider, data.callback_port,
        (data.exchange && data.exchange.state) || '');

      // Step 2: Open the auth URL in a popup window
      // For Claude: redirects to console.anthropic.com which shows code#state
      // For Codex: local desktop flows auto-relay; remote flows stop on the
      // fixed localhost callback and the complete address-bar URL is pasted.
      var popup = null;
      if (data.auth_url) {
        var w = 600, h = 700;
        var left = (screen.width - w) / 2, top = (screen.height - h) / 2;
        popup = window.open(data.auth_url, 'oauth_' + provider,
          'width=' + w + ',height=' + h + ',left=' + left + ',top=' + top +
          ',menubar=no,toolbar=no,status=no,scrollbars=yes');

        if (!popup || popup.closed) {
          // Popup blocked — fall back to new tab
          popup = null;
          window.open(data.auth_url, '_blank');
        }
      }

      // Update UI to waiting state
      _updateOAuthCard(provider, { status: 'waiting_callback' });

      // Keep recovery out of the happy path. The user should see it only
      // when the browser blocked the popup or the provider explicitly uses a
      // console flow that requires pasting an authorization result.
      var manualDiv = document.getElementById('oauth' + capProvider + 'Manual');
      if (manualDiv) {
        var manualNeeded = !popup || data.redirect_mode === 'console' ||
          data.redirect_mode === 'manual';
        manualDiv.style.display = manualNeeded ? '' : 'none';
        var authUrlInput = document.getElementById('oauth' + capProvider + 'AuthUrl');
        if (authUrlInput && data.auth_url) authUrlInput.value = data.auth_url;
      }
      // Describe the flow the user is ACTUALLY about to walk, and expose the
      // way out of it. During a loopback flow the paste instructions are
      // FALSE (the provider redirects to localhost and never renders a
      // code), so showing them unchanged would hand the user a task that
      // cannot be completed.
      _oauthApplyRedirectMode(provider, data.redirect_mode);

      // ── Detect popup closed → auto-reset ONLY if manual box not used ──
      if (popup) {
        var popupCheckInterval = setInterval(function() {
          /* Self-terminate once the login resolves (success / error / cancel):
           *   the old code only stopped on popup.closed, leaking a 1s interval
           *   for every Connect click that never closed its popup (). */
          var badgeNow = document.getElementById('oauth' + capProvider + 'Status');
          if (badgeNow && !badgeNow.classList.contains('pending')) {
            clearInterval(popupCheckInterval);
            return;
          }
          if (!popup || popup.closed) {
            clearInterval(popupCheckInterval);
            // Don't reset if manual paste box is visible (user may be pasting code)
            var manualInput = document.getElementById('oauth' + capProvider + 'ManualUrl');
            if (manualInput && manualInput.value.trim()) return;  // user is typing
            // Only reset if still in waiting state (not already succeeded)
            var badge = document.getElementById('oauth' + capProvider + 'Status');
            if (badge && badge.classList.contains('pending')) {
              // The automatic route did not finish. Reveal recovery only now,
              // after the user has closed the authorization window.
              if (manualDiv) manualDiv.style.display = '';
              _oauthApplyRedirectMode(provider, data.redirect_mode);
              // Don't reset — just update button to allow retry
              var loginBtn2 = document.getElementById('oauth' + capProvider + 'LoginBtn');
              if (loginBtn2) {
                loginBtn2.disabled = false;
                loginBtn2.textContent = t('settings.oauthReopenPopup');
                loginBtn2.onclick = function() {
                  // Re-open popup with same auth URL, don't create new flow
                  var w2 = 600, h2 = 700;
                  var left2 = (screen.width - w2) / 2, top2 = (screen.height - h2) / 2;
                  window.open(data.auth_url, 'oauth_' + provider,
                    'width=' + w2 + ',height=' + h2 + ',left=' + left2 + ',top=' + top2 +
                    ',menubar=no,toolbar=no,status=no,scrollbars=yes');
                };
              }
            }
          }
        }, 1000);
      }
    })
    .catch(function(e) {
      console.error('[OAuth] Login error:', e);
      showAlert(t('settings.oauthLoginReqFailed', { error: e.message }));
      if (loginBtn) { loginBtn.disabled = false; loginBtn.textContent = provider === 'codex' ? t('settings.oauthLoginChatGPT') : t('settings.oauthLoginClaude'); }
    });
}

// ── Device-authorization login (Codex) ──
// The loopback callback (localhost:1455) only resolves when the browser and
// the Tofu server share a machine. The device flow never touches a localhost
// redirect: the server mints a user code, the user enters it at the
// verification URL in ANY browser (phone included), and the server's poll
// thread completes the exchange — we just watch the status projection.
var _oauthDevicePollTimer = null;

function _oauthDeviceLogin(provider) {
  if (provider !== 'codex') return;
  var deviceBtn = document.getElementById('oauthCodexDeviceBtn');
  if (deviceBtn && deviceBtn.disabled) return;
  if (deviceBtn) { deviceBtn.disabled = true; deviceBtn.textContent = t('settings.oauthPreparing'); }

  function _doDeviceRequest(useGet) {
    if (useGet) {
      console.warn('[OAuth] POST failed, retrying as GET for /api/v4/oauth/device-login');
      return Api.oauth.deviceLoginGet(provider);
    }
    return Api.oauth.deviceLoginPost(provider);
  }
  _doDeviceRequest(false)
    .then(function(r) {
      if (r.status === 404 || r.status === 405) return _doDeviceRequest(true);
      return r;
    })
    .then(function(r) {
      if (!r.ok) {
        return r.text().then(function(tx) {
          var body; try { body = JSON.parse(tx); } catch (parseError) { body = null; }
          var detail = body &&
            (errorEnvelopeMessage(body.error) || body.detail);
          var err = new Error('HTTP ' + r.status + ': ' +
            String(detail || tx).slice(0, 200));
          err._httpStatus = r.status;
          if (body && typeof body.status_code !== 'undefined') {
            err._statusCode = body.status_code;
          }
          throw err;
        });
      }
      return r.json();
    })
    .then(function(data) {
      if (deviceBtn) { deviceBtn.disabled = false; deviceBtn.textContent = t('settings.oauthDeviceLogin'); }
      if (!data || data.error) {
        showAlert(t('settings.oauthLoginFailed', {
          error: (data && errorEnvelopeMessage(data.error)) || 'unknown',
        }));
        return;
      }
      if (data.status === 'success') {
        _hideDevicePanel();
        var reused = Object.assign({}, data, { authenticated: true });
        _updateOAuthCard(provider, reused);
        _autoConfigureOAuthProvider(provider, reused);
        return;
      }
      _showDevicePanel(data.user_code, data.verification_url);
      _updateOAuthCard(provider, {
        status: 'waiting_callback',
        device: { user_code: data.user_code, verification_url: data.verification_url },
      });
      _startDeviceStatusPoll(provider);
    })
    .catch(function(e) {
      console.error('[OAuth] Device login error:', e);
      if (deviceBtn) { deviceBtn.disabled = false; deviceBtn.textContent = t('settings.oauthDeviceLogin'); }
      if (e && (e._httpStatus === 503 || e._statusCode === 0)) {
        // Deviceauth must be minted by the server. When every server/agent
        // route is down, fall back to the browser-network PKCE flow instead
        // of ending on a raw HTTP 400/503. The remote callback-copy box makes
        // the fixed localhost redirect usable without any local listener.
        Promise.resolve(showAlert(t('settings.oauthDeviceFallback'))).then(
          function() { _oauthLogin(provider); },
          function() { _oauthLogin(provider); });
        return;
      }
      showAlert(t('settings.oauthLoginReqFailed', { error: e.message }));
    });
}

function _showDevicePanel(userCode, verificationUrl) {
  var panel = document.getElementById('oauthCodexDevice');
  if (!panel) return;
  panel.style.display = '';
  var codeEl = document.getElementById('oauthCodexDeviceCode');
  if (codeEl) codeEl.textContent = userCode || '';
  var link = document.getElementById('oauthCodexDeviceLink');
  if (link && verificationUrl) link.href = verificationUrl;
  // The loopback manual box is FALSE during a device flow — the provider
  // never redirects anywhere, it renders a code-entry page.
  var manual = document.getElementById('oauthCodexManual');
  if (manual) manual.style.display = 'none';
}

function _hideDevicePanel() {
  var panel = document.getElementById('oauthCodexDevice');
  if (panel) panel.style.display = 'none';
}

function _oauthCopyDeviceCode(button) {
  var codeEl = document.getElementById('oauthCodexDeviceCode');
  if (!codeEl || !codeEl.textContent) return;
  function _copied() {
    button.textContent = t('settings.oauthCopied');
    setTimeout(function() { button.textContent = t('settings.oauthCopyCode'); }, 1500);
  }
  if (navigator.clipboard && navigator.clipboard.writeText) {
    navigator.clipboard.writeText(codeEl.textContent).then(_copied, function() {});
  } else {
    var tmp = document.createElement('textarea');
    tmp.value = codeEl.textContent;
    document.body.appendChild(tmp);
    tmp.select();
    try { document.execCommand('copy'); _copied(); } catch (e) {}
    document.body.removeChild(tmp);
  }
}

function _startDeviceStatusPoll(provider) {
  _stopDeviceStatusPoll();
  _oauthDevicePollTimer = setInterval(function() {
    Api.oauth.status()
      .then(function(data) {
        if (!data) return;
        var s = data[provider];
        _updateOAuthCard(provider, s);
        var terminal = !s || s.authenticated ||
          ['error', 'timeout', 'not_started'].indexOf(s.status) >= 0 ||
          !s.device;
        if (terminal) {
          _stopDeviceStatusPoll();
          if (s && s.authenticated) _autoConfigureOAuthProvider(provider, s);
        }
      })
      .catch(function() {});
  }, 3000);
}

function _stopDeviceStatusPoll() {
  if (_oauthDevicePollTimer) {
    clearInterval(_oauthDevicePollTimer);
    _oauthDevicePollTimer = null;
  }
}

async function _oauthLogout(provider) {
  if (!await showConfirm(t('settings.oauthLogoutConfirm', { provider: (provider === 'codex' ? 'ChatGPT' : 'Claude') }))) return;

  // Try POST first; if proxy returns 405, fall back to GET with query params
  function _doLogoutRequest(useGet) {
    if (useGet) {
      console.warn('[OAuth] POST failed, retrying as GET for /api/oauth/logout');
      return Api.oauth.logoutGet(provider);
    }
    return Api.oauth.logoutPost(provider);
  }
  _doLogoutRequest(false)
    .then(function(r) {
      if (r.status === 404 || r.status === 405) return _doLogoutRequest(true);
      return r;
    })
    .then(function(r) { return r.json(); })
    .then(function() {
      _oauthClearPendingFlow(provider);
      _updateOAuthCard(provider, { status: 'not_started', authenticated: false });
      if (typeof _refreshSubscriptionModelCatalog === 'function') {
        return _refreshSubscriptionModelCatalog();
      }
      return null;
    })
    .catch(function(e) {
      showAlert(t('settings.oauthLogoutFailed', { error: e.message }));
    });
}

var _oauthResetConsumeInFlight = false;
function _oauthConsumeReset() {
  if (_oauthResetConsumeInFlight) return;
  var confirmed = typeof showConfirm === 'function'
    ? showConfirm(t('settings.oauthResetRedeemConfirm'), {
        okText: t('settings.oauthResetRedeemButton'),
      })
    : Promise.resolve(true);
  confirmed.then(function(ok) {
    if (!ok) return;
    _oauthResetConsumeInFlight = true;
    return Api.oauth.resetOfferConsume()
      .then(function(resp) {
        if (resp && typeof resp.json === 'function') {
          return resp.json().catch(function() { return null; });
        }
        return resp;
      })
      .then(function(body) {
        if (body && body.ok) {
          showAlert(t('settings.oauthResetRedeemSuccess'));
          return;
        }
        var code = body && body.error ? String(body.error) : '';
        var key = 'settings.oauthResetRedeemFailed';
        if (code === 'none_available' || code === 'nothing_to_reset' ||
            code === 'already_redeemed') {
          key = 'settings.oauthResetRedeemGone';
        } else if (code === 'unresolved') {
          key = 'settings.oauthResetRedeemUnresolved';
        }
        showAlert(t(key));
      })
      .catch(function(e) {
        console.warn('[OAuth] reset consume failed:', e);
        showAlert(t('settings.oauthResetRedeemFailed'));
      })
      .then(function() {
        _oauthResetConsumeInFlight = false;
        _loadOAuthStatus();
      });
  });
}
function _oauthCopyAuthLink(button, provider) {
  const capProvider = provider === 'codex' ? 'Codex' : 'Claude';
  const input = document.getElementById('oauth' + capProvider + 'AuthUrl');
  if (!input) return;
  input.select();
  document.execCommand('copy');
  button.textContent = t('settings.oauthCopied');
  setTimeout(function() {
    button.textContent = t('settings.oauthCopyLink');
  }, 1500);
}

function _oauthManualSubmit(provider) {
  var capP = provider === 'codex' ? 'Codex' : 'Claude';
  var input = document.getElementById('oauth' + capP + 'ManualUrl');
  if (!input || !input.value.trim()) {
    showAlert(t('settings.oauthPasteCodePrompt'));
    return;
  }
  var val = input.value.trim();

  // Support multiple formats:
  // 1. Full callback URL: http://localhost:PORT/callback?code=XXX&state=YYY
  // 2. code#state format (shown by Anthropic console after auth)
  // 3. Raw authorization code
  var code = '', state = '';
  if (val.indexOf('http') === 0) {
    try {
      var u = new URL(val);
      code = u.searchParams.get('code') || '';
      state = u.searchParams.get('state') || '';
    } catch (e) { code = ''; }
    if (!code) { showAlert(t('settings.oauthNoCodeInUrl')); return; }
  } else if (val.indexOf('#') > 0) {
    // code#state format from Anthropic console
    var parts = val.split('#');
    code = parts[0];
    state = parts[1] || '';
  } else {
    code = val;
  }

  // Browser-first exchange (bypasses the server's geo-block), server fallback.
  _completeLogin(provider, code, state, { manual: true });
}

function _autoConfigureOAuthProvider(provider, status) {
  var name = provider === 'codex' ? 'ChatGPT Plus' : 'Claude Pro';
  var el = document.getElementById('settingsStatusHint');
  if (el) {
    el.textContent = t('settings.oauthAutoConfigured', { name: name });
    el.style.color = '#28a745';
  }
  // The backend auto-provisions a managed provider on login; refresh the
  // providers list so the new models appear without a manual reload.
  if (typeof _refreshSubscriptionModelCatalog === 'function') {
    _refreshSubscriptionModelCatalog().then(function(cfg) {
      if (!cfg) return;
      _loadOAuthStatus();
    }).catch(function(e) {
      console.warn('[OAuth] subscription catalogue refresh failed:', e);
      showAlert(t('settings.oauthCatalogRepairFailed'));
    });
  }
}

// ── CatPaw (Credit Hub) one-click local import ──
// Not OAuth: the CatPaw/mcopilot CLI is the only credential issuer (it writes
// a fixed loopback config after one interactive login). The card reads a
// secret-free availability verdict and offers a one-click import; the token
// itself never crosses the API. Roster/base_url come from the catpaw template.

function _catpawRender(status) {
  var badge = document.getElementById('oauthCatpawStatus');
  var importBtn = document.getElementById('oauthCatpawImportBtn');
  var disconnectBtn = document.getElementById('oauthCatpawDisconnectBtn');
  var hint = document.getElementById('oauthCatpawHint');
  if (!badge) return;
  badge.title = '';
  var connected = !!(status && status.connected);
  var available = !!(status && status.available);
  if (connected) {
    _catpawAutoTried = false;  // ready for a future disconnect→reconnect cycle
    badge.textContent = t('settings.oauthModelsReady', { n: status.model_count || 0 });
    badge.className = 'oauth-status-badge authenticated';
    if (importBtn) importBtn.style.display = 'none';
    if (disconnectBtn) disconnectBtn.style.display = '';
    if (hint) hint.style.display = 'none';
  } else if (available) {
    badge.textContent = t('settings.oauthCatpawReady');
    badge.className = 'oauth-status-badge pending';
    if (importBtn) { importBtn.style.display = ''; importBtn.disabled = false; importBtn.textContent = t('settings.oauthCatpawImport'); }
    if (disconnectBtn) disconnectBtn.style.display = 'none';
    if (hint) hint.style.display = 'none';
  } else {
    // No credential: button now triggers the guided CLI login (not disabled).
    badge.textContent = t('settings.oauthCatpawUnavailable');
    badge.className = 'oauth-status-badge';
    if (importBtn) { importBtn.style.display = ''; importBtn.disabled = false; importBtn.textContent = t('settings.oauthCatpawLogin'); }
    if (disconnectBtn) disconnectBtn.style.display = 'none';
    if (hint) hint.style.display = '';
  }
}

// Auto-import in flight / done flags so the poll + initial load don't double-fire.
var _catpawAutoTried = false;
var _catpawPollTimer = null;

function _catpawLoadStatus() {
  if (!document.getElementById('oauthCatpawCard')) return;
  Api.oauth.catpawImportStatus()
    .then(function(data) {
      data = data || { available: false };
      // Fully automatic path: credential on disk, not yet connected → import
      // without any click. Explicitly authorized by the user.
      if (data.available && !data.connected && !_catpawAutoTried) {
        _catpawAutoTried = true;
        _catpawDoImport(true);
        return;
      }
      if (data.connected) _catpawStopPoll();
      _catpawRender(data);
    })
    .catch(function() { _catpawRender({ available: false }); });
}

// silent=true suppresses failure alerts (auto path falls back to manual button).
function _catpawDoImport(silent) {
  var importBtn = document.getElementById('oauthCatpawImportBtn');
  if (importBtn) { importBtn.disabled = true; importBtn.textContent = t('settings.oauthPreparing'); }
  return Api.oauth.catpawImportApply()
    .then(function(r) { return r && typeof r.json === 'function' ? r.json() : r; })
    .then(function(data) {
      if (importBtn) { importBtn.disabled = false; importBtn.textContent = t('settings.oauthCatpawImport'); }
      if (!data || data.error || data.ok === false) {
        if (!silent) showAlert(t('settings.oauthCatpawImportFailed'));
        _catpawLoadStatus();
        return;
      }
      _catpawRender(data);
      _loadOAuthStatus();
    })
    .catch(function() {
      if (importBtn) { importBtn.disabled = false; importBtn.textContent = t('settings.oauthCatpawImport'); }
      if (!silent) showAlert(t('settings.oauthCatpawImportFailed'));
      _catpawLoadStatus();
    });
}

function _catpawStopPoll() {
  if (_catpawPollTimer) { clearInterval(_catpawPollTimer); _catpawPollTimer = null; }
}

// Poll while a CLI login is in progress; auto-import the instant the
// credential lands. Bounded so we never poll forever.
function _catpawStartPoll() {
  _catpawStopPoll();
  var attempts = 0;
  _catpawPollTimer = setInterval(function() {
    attempts += 1;
    if (attempts > 60 || !document.getElementById('oauthCatpawCard')) { _catpawStopPoll(); return; }
    Api.oauth.catpawImportStatus()
      .then(function(data) {
        data = data || {};
        if (data.available && !data.connected) {
          _catpawStopPoll();
          _catpawAutoTried = true;
          _catpawDoImport(true);
        } else if (data.connected) {
          _catpawStopPoll();
          _catpawRender(data);
        }
      })
      .catch(function() {});
  }, 3000);
}

// One click when no credential: kick the CLI login, open its SSO link, poll.
function _catpawLogin() {
  var btn = document.getElementById('oauthCatpawImportBtn');
  if (btn) { btn.disabled = true; btn.textContent = t('settings.oauthPreparing'); }
  Api.oauth.catpawImportLogin()
    .then(function(data) {
      data = data || {};
      if (data.login_url) {
        try { window.open(data.login_url, '_blank', 'noopener'); } catch (e) {}
        _catpawStartPoll();
        var hint = document.getElementById('oauthCatpawHint');
        if (hint) { hint.textContent = t('settings.oauthCatpawLoginWaiting'); hint.style.display = ''; }
      } else {
        // CLI missing or link not captured → show manual instructions.
        _catpawRender({ available: false, installed: !!data.installed });
      }
      if (btn) { btn.disabled = false; btn.textContent = t('settings.oauthCatpawImport'); }
    })
    .catch(function() {
      if (btn) { btn.disabled = false; btn.textContent = t('settings.oauthCatpawImport'); }
      _catpawRender({ available: false });
    });
}

function _catpawImport() {
  // Manual click: if a credential is already on disk, import; otherwise treat
  // the click as "get me logged in" and run the CLI login flow.
  Api.oauth.catpawImportStatus()
    .then(function(s) {
      if (s && s.available) { _catpawDoImport(false); }
      else { _catpawLogin(); }
    })
    .catch(function() { _catpawLogin(); });
}

async function _catpawDisconnect() {
  if (!await showConfirm(t('settings.oauthCatpawDisconnectConfirm'))) return;
  // The relay provider is a normal model-routing aggregate; deleting it is
  // the disconnect. Find it by template brand and reuse the CAS delete.
  Api.modelRouting.get()
    .then(function(data) {
      var doc = data && data.model_routing;
      var revision = data && data.revision;
      var row = ((doc && doc.providers) || []).find(function(p) {
        return p && p.brand === 'catpaw';
      });
      if (!row) { _catpawLoadStatus(); return null; }
      return Api.modelRouting.deleteProvider(row.provider_id, revision)
        .then(function() {
          if (typeof _loadModelRoutingAuthority === 'function') {
            return _loadModelRoutingAuthority();
          }
          return null;
        })
        .then(function() { _catpawLoadStatus(); });
    })
    .catch(function(e) {
      showAlert(t('settings.oauthLogoutFailed', { error: (e && e.message) || 'unknown' }));
      _catpawLoadStatus();
    });
}
/* ===== migrated source: settings/mcp.js ===== */
/* ═══════════════════════════════════════════════════════════════════
   settings/mcp — extracted from settings.js (split 2026-05-28)

   MCP catalog UI: render, install modal, save server, reconnect.

   This file is concatenated by Vite's module graph — symbols share
   the same window scope as every other frontend/src/runtime/*.js file. No
   exports / imports needed.
   ═══════════════════════════════════════════════════════════════════ */

// ══════════════════════════════════════════════════════
//  MCP Apps Tab — App Store style UI
// ══════════════════════════════════════════════════════

/** Cached catalog & state */
var _mcpCatalog = [];
var _mcpActiveCategory = 'all';
var _mcpScope = 'all';         // 'all' | 'installed' | 'available'
var _mcpSearchQuery = '';
var _mcpInstallTarget = null;  // CatalogEntry being installed
var _mcpInstallIsReinstall = false;  // true = editing existing (stored env will be honoured)
var _mcpBreakerRefreshTimer = null;  // single-shot re-fetch while a breaker is counting down
var _mcpBreakerTickTimer = null;     // 1s interval that ticks the live "retry in N" countdowns
/* In-flight per-card operations (): serverId →
 *   'connecting' | 'uninstalling' | 'installing'. Consulted by
 *   _renderMcpCatalog, so the busy state SURVIVES any concurrent repopulate
 *   (the breaker-refresh timer fires _populateMcpTab mid-operation) — a
 *   one-off DOM patch would be silently reverted by that re-render. */
var _mcpPending = {};
/* Per-tool toggle state (): which cards have their tool
 * list expanded, and the fetched per-server tool rows. Both survive the
 * full-grid re-render that every populate/breaker-refresh triggers, for the
 * same reason _mcpPending does. */
var _mcpToolsOpen = {};
var _mcpToolsCache = {};

/* Latest upstream-version sightings, filled ASYNCHRONOUSLY after the grid
 * first paints (the tab would otherwise block on one npm/PyPI round-trip
 * per installed server). serverId → check_server_update dict; a card only
 * grows an update button once this lands and re-renders. */
var _mcpUpdates = {};
var _mcpUpdatesInFlight = false;

function _mcpSetPending(serverId, kind) {
  _mcpPending[serverId] = kind;
  _renderMcpCatalog();
}
function _mcpClearPending(serverId, skipRender) {
  if (delete _mcpPending[serverId] && !skipRender) _renderMcpCatalog();
}


function _mcpPendingLabel(pending) {
  return pending === 'connecting' ? t('mcp.connecting')
    : pending === 'installing' ? t('mcp.installing')
    : pending === 'updating' ? t('mcp.updating')
    : t('mcp.uninstalling');
}

/**
 * Refresh the upstream-version sightings, then re-render so eligible cards
 * grow their update button. Fire-and-forget from _populateMcpTab — the
 * catalog render never waits on registry round-trips.
 */
async function _mcpCheckUpdates() {
  if (_mcpUpdatesInFlight) return;
  _mcpUpdatesInFlight = true;
  try {
    var r = await Api.mcp.updatesCheck();
    if (!r || !r.ok) return;
    var data = await r.json();
    _mcpUpdates = (data && data.updates) || {};
    _renderMcpCatalog();
  } catch (e) {
    debugLog('[MCP] update check failed: ' + ((e && e.message) || e), 'warning');
  } finally {
    _mcpUpdatesInFlight = false;
  }
}

/**
 * Load MCP tab data — fetch catalog with install/connect status.
 */
async function _populateMcpTab() {
  var grid = document.getElementById('mcpCatalogGrid');
  if (grid) grid.innerHTML = '<p class="stg-loading">' + escapeHtml(t('mcp.loading')) + '</p>';
  try {
    var r = await Api.mcp.catalogList();
    if (!r || !r.ok) throw new Error('HTTP ' + (r ? r.status : 'no response'));
    var data = await r.json();
    _mcpCatalog = data.catalog || [];
    if (data.mcp_tool_summary
        && typeof runtimeScope.applyMcpToolSummary === 'function') {
      runtimeScope.applyMcpToolSummary(data.mcp_tool_summary);
    }

    _renderMcpCategoryBar();
    _renderMcpCatalog();
    _renderMcpInstalled();
    _mcpUpdateToolCount();
    _mcpCheckUpdates();   // async — cards gain update buttons on re-render
  } catch (e) {
    if (grid) grid.innerHTML = '<p class="stg-empty">' + escapeHtml(t('mcp.loadFailed', { err: e.message })) + '</p>';
    debugLog('[MCP] Failed to load catalog: ' + e.message, 'error');
  }
}

/** Update the tool count badge. */
function _mcpUpdateToolCount() {
  var badge = document.getElementById('mcpToolCount');
  if (!badge) return;
  // Count what the MODEL actually gets: discovered minus per-tool-disabled.
  var total = 0;
  _mcpCatalog.forEach(function(e) {
    total += Math.max(0, (e.tools_count || 0) - (e.disabled_tools || []).length);
  });
  badge.textContent = t('mcp.toolsCount', { n: total });
}

/**
 * Render category filter pills.
 *
 * The pill set is DERIVED from the categories the catalog actually returned,
 * never from a hand-copied list. A literal whitelist here silently swallowed
 * every category the backend added later: `lib/mcp/registry.py::CATEGORIES`
 * grew to 12 while this list still held 10, so 'Local Life & Travel (China)'
 * (5 entries) and 'Science & Research' (2) had NO pill at all — their cards
 * existed but were unreachable by filtering. `_CAT_ORDER` is a display
 * PREFERENCE only; anything missing from it still renders, appended
 * alphabetically after the known ones.
 */
var _CAT_ORDER = ['Development','Data & DB','Communication','Search & Web',
                  'Productivity','DevOps','Finance','Design',
                  'Science & Research','Local Life & Travel (China)',
                  'Other','Custom'];

function _mcpOrderedCategories(cats) {
  var known = _CAT_ORDER.filter(function(c) { return cats[c]; });
  var extra = Object.keys(cats).filter(function(c) {
    return _CAT_ORDER.indexOf(c) === -1;
  }).sort();
  return known.concat(extra);
}

function _renderMcpCategoryBar() {
  var bar = document.getElementById('mcpCategoryBar');
  if (!bar) return;
  var cats = {};
  _mcpCatalog.forEach(function(e) {
    var c = e.category || 'Other';
    cats[c] = (cats[c] || 0) + 1;
  });
  var html = '<button class="mcp-cat-pill' + (_mcpActiveCategory === 'all' ? ' active' : '') + '" data-tofu-action="_mcpSetCategory(\'all\')">' + escapeHtml(t('mcp.scopeAll')) + ' <span class="mcp-cat-count">' + _mcpCatalog.length + '</span></button>';
  _mcpOrderedCategories(cats).forEach(function(c) {
    html += '<button class="mcp-cat-pill' + (_mcpActiveCategory === c ? ' active' : '') + '" data-tofu-action="_mcpSetCategory(\'' + escapeHtml(c).replace(/'/g, "\\'") + '\')">' + escapeHtml(c) + ' <span class="mcp-cat-count">' + cats[c] + '</span></button>';
  });
  bar.innerHTML = html;
}

function _mcpSetCategory(cat) {
  _mcpActiveCategory = cat;
  _renderMcpCategoryBar();
  _renderMcpCatalog();
}

/** Switch the installed/available scope filter and re-render. */
function _mcpSetScope(scope) {
  _mcpScope = scope;
  var tabs = document.getElementById('mcpScopeTabs');
  if (tabs) {
    tabs.querySelectorAll('.skills-scope-tab').forEach(function(t) {
      t.classList.toggle('active', t.getAttribute('data-scope') === scope);
    });
  }
  _renderMcpCatalog();
}

function _mcpFilterCatalog(query) {
  _mcpSearchQuery = (query || '').toLowerCase().trim();
  _renderMcpCatalog();
}

/** Filter catalog entries by active category + search query. */
function _mcpFilteredCatalog() {
  return _mcpCatalog.filter(function(e) {
    if (_mcpActiveCategory !== 'all' && e.category !== _mcpActiveCategory) return false;
    // Scope filter: an entry counts as "installed" if it is configured
    // (installed) regardless of live connection state.
    if (_mcpScope === 'installed' && !e.installed) return false;
    if (_mcpScope === 'available' && e.installed) return false;
    if (_mcpSearchQuery) {
      var hay = (e.name + ' ' + e.description + ' ' + (e.tags || []).join(' ')).toLowerCase();
      return hay.indexOf(_mcpSearchQuery) !== -1;
    }
    return true;
  });
}

/**
 * Format a number of seconds-until-retry into a short human label.
 * `secs <= 0` → "retrying…"; under a minute → "retry in Ns"; else
 * "retry in N min" (rounded up).
 */
function _mcpRetryLabel(secs) {
  secs = Math.max(0, Math.round(secs || 0));
  if (secs <= 0) return t('mcp.retryNow');
  if (secs < 60) return t('mcp.retryInSec').replace('{n}', String(secs));
  return t('mcp.retryInMin').replace('{n}', String(Math.ceil(secs / 60)));
}

/**
 * Build the inner HTML for a live countdown span. The span carries the
 * absolute retry deadline (epoch ms) in ``data-retry-at`` so a 1s ticker
 * (`_mcpTickBreakers`) can recompute the remaining time and update the
 * text in place — no full grid re-render. Returns '' for no breaker.
 *
 * `breaker` shape (from the backend): {failures, retry_in, next_retry_ts}.
 * We derive the deadline from `retry_in` relative to *now* rather than
 * trusting `next_retry_ts` (server/client clocks may differ).
 */
function _mcpBreakerCountdownSpan(breaker) {
  if (!breaker) return '';
  var secs = Math.max(0, breaker.retry_in || 0);
  var deadline = Date.now() + secs * 1000;
  return '<span class="mcp-breaker-countdown" data-retry-at="' + deadline + '">' +
    escapeHtml(_mcpRetryLabel(secs)) + '</span>';
}

/**
 * Entries worth suggesting to someone who has installed nothing yet.
 *
 * Restricted to servers a user can provably finish installing on their own:
 * either they need no credential at all, or every required credential
 * declares an `obtain_url` (so the card can hand them a real link). Anything
 * gated behind a process we cannot show a route for is EXCLUDED — recommending
 * it would send the user down a path that dead-ends, which is worse than not
 * recommending at all.
 *
 * Deliberately NOT an in-conversation intent detector: a phrase-matching
 * trigger was measured at 60% false positives on a sibling epic
 * (), so suggestions stay inside the panel the user
 * already opened.
 */
function _mcpSelfServeSuggestions(limit) {
  var out = _mcpCatalog.filter(function(e) {
    if (e.installed || e.custom) return false;
    var specs = e.env_specs || [];
    var required = specs.filter(function(s) { return s.required; });
    if (required.length === 0) return true;   // nothing to obtain
    return required.every(function(s) { return !!s.obtain_url; });
  });
  out.sort(function(a, b) {
    if (a.featured && !b.featured) return -1;
    if (!a.featured && b.featured) return 1;
    return (a.name || '').localeCompare(b.name || '');
  });
  return out.slice(0, limit || 6);
}

/** Render the main catalog grid. */
function _renderMcpCatalog() {
  var grid = document.getElementById('mcpCatalogGrid');
  if (!grid) return;
  var items = _mcpFilteredCatalog();
  if (items.length === 0) {
    var emptyMsg = _mcpScope === 'installed' ? t('mcp.emptyInstalled')
      : _mcpScope === 'available' ? t('mcp.emptyAvailable')
      : t('mcp.emptyNoMatch');
    var emptyHtml = '<p class="stg-empty">' + emptyMsg + '</p>';
    // "Nothing installed" is the one empty state where the user has a next
    // action rather than a failed filter — offer it instead of a dead end.
    if (_mcpScope === 'installed') {
      var picks = _mcpSelfServeSuggestions(6);
      if (picks.length > 0) {
        emptyHtml += '<div class="mcp-suggest">';
        emptyHtml += '<div class="mcp-suggest-title">' +
          escapeHtml(t('mcp.suggestTitle')) + '</div>';
        emptyHtml += '<div class="mcp-suggest-row">';
        picks.forEach(function(e) {
          emptyHtml += '<button class="mcp-suggest-chip" data-tofu-action="_mcpSetScope(\'available\');_mcpFilterCatalog(' +
            JSON.stringify(e.name || e.id).replace(/"/g, '&quot;') + ')" title="' +
            escapeHtml(e.description || '') + '">' +
            (e.icon && !/^</.test(e.icon) ? escapeHtml(e.icon) + ' ' : '') +
            escapeHtml(e.name || e.id) + '</button>';
        });
        emptyHtml += '</div></div>';
      }
    }
    grid.innerHTML = emptyHtml;
    return;
  }
  // Show featured first, then alphabetical
  items.sort(function(a, b) {
    if (a.featured && !b.featured) return -1;
    if (!a.featured && b.featured) return 1;
    return (a.name || '').localeCompare(b.name || '');
  });
  var REPO_SVG = '<svg width="11" height="11" viewBox="0 0 16 16" fill="currentColor"><path d="M8 0C3.58 0 0 3.58 0 8c0 3.54 2.29 6.53 5.47 7.59.4.07.55-.17.55-.38 0-.19-.01-.82-.01-1.49-2.01.37-2.53-.49-2.69-.94-.09-.23-.48-.94-.82-1.13-.28-.15-.68-.52-.01-.53.63-.01 1.08.58 1.23.82.72 1.21 1.87.87 2.33.66.07-.52.28-.87.51-1.07-1.78-.2-3.64-.89-3.64-3.95 0-.87.31-1.59.82-2.15-.08-.2-.36-1.02.08-2.12 0 0 .67-.21 2.2.82.64-.18 1.32-.27 2-.27.68 0 1.36.09 2 .27 1.53-1.04 2.2-.82 2.2-.82.44 1.1.16 1.92.08 2.12.51.56.82 1.27.82 2.15 0 3.07-1.87 3.75-3.65 3.95.29.25.54.73.54 1.48 0 1.07-.01 1.93-.01 2.2 0 .21.15.46.55.38A8.013 8.013 0 0016 8c0-4.42-3.58-8-8-8z"/></svg>';
  var html = '';
  items.forEach(function(e) {
    var installed = e.installed;
    var connected = e.connected;
    // A breaker is "active" only when the server is installed but not
    // currently connected and its automatic reconnect is failing.
    var breaker = (!connected && installed) ? e.breaker : null;
    // Credential health is a SECOND axis: a server can be connected (live
    // subprocess) yet its stored session cookie/token has expired, so every
    // real tool call fails. Only surface it while connected — a disconnected
    // card already tells its own story via breaker / idle state.
    var credExpired = connected && e.cred_health && e.cred_health.status === 'expired';
    // Older peers remain fully usable through the SDK v2 negotiation fallback.
    // Surface one calm, actionable note instead of downgrading the healthy ON
    // state or emitting repeated toast/log warnings.
    var legacyCompat = connected && e.compatibility_notice &&
      e.compatibility_notice.kind === 'legacy_protocol' &&
      e.compatibility_notice.update_recommended === true;
    /* An in-flight click operation outranks every derived state — the
     *   card paints the busy label + NO action buttons until the handler
     *   clears it (success → repopulate; failure → restore). */
    var pending = _mcpPending[e.id] || null;
    var stateClass = connected ? (credExpired ? ' connected cred-expired' : ' connected')
      : breaker ? ' installed reconnecting'
      : installed ? ' installed' : '';
    if (pending) stateClass += ' mcp-pending';
    /* An open tools drawer spans the full grid row (.mcp-app-card.tools-open
     * + grid-auto-flow:dense) so a tall expanded card never stretches its
     * grid-row neighbour into a blank void. */
    var toolsOpen = connected && !pending && !!_mcpToolsOpen[e.id];
    if (toolsOpen) stateClass += ' tools-open';
    html += '<div class="mcp-app-card' + stateClass + '">';
    html += '<div class="mcp-app-icon">' + (e.icon || Icon('plug', 26)) + '</div>';
    html += '<div class="mcp-app-name"><span class="mcp-app-name-text">' + escapeHtml(e.name) + '</span>';
    if (pending) {
      html += '<span class="mcp-app-status pending">' + escapeHtml(
        _mcpPendingLabel(pending)) + '</span>';
    } else if (credExpired) {
      html += '<span class="mcp-app-status cred-expired" title="' +
        escapeHtml(t('mcp.credExpiredTitle')) + '">' +
        Icon('alertTriangle', 12) + ' ' + escapeHtml(t('mcp.credExpired')) + '</span>';
    } else if (connected) {
      html += '<span class="mcp-app-status on"><span class="dot"></span>' + escapeHtml(t('mcp.statusOn')) + '</span>';
    } else if (breaker) {
      html += '<span class="mcp-app-status reconnecting" title="' +
        escapeHtml(t('mcp.reconnecting') + ' · ' + t('mcp.retryFailCount').replace('{n}', String(breaker.failures || 0))) +
        '">⟳ ' + _mcpBreakerCountdownSpan(breaker) + '</span>';
    } else if (installed) {
      html += '<span class="mcp-app-status off">' + escapeHtml(t('mcp.statusIdle')) + '</span>';
    }
    html += '</div>';
    html += '<div class="mcp-app-desc">' + escapeHtml(e.description || '') + '</div>';
    // Getting-started note (how to obtain a key, what the server can/can't do).
    // This field was authored in lib/mcp/registry.py from the start but had NO
    // renderer anywhere in the frontend, so every note written for a user was
    // invisible to them.
    if (e.install_note) {
      html += '<div class="mcp-app-note">' + escapeHtml(e.install_note) + '</div>';
    }
    if (legacyCompat) {
      html += '<div class="mcp-app-compat-notice" title="' +
        escapeHtml(t('mcp.legacyProtocolTitle', {
          version: e.compatibility_notice.protocol_version || e.protocol_version || '?',
          target: e.compatibility_notice.target_protocol || '2026-07-28'
        })) + '">' + Icon('info', 12) + '<span>' +
        escapeHtml(t('mcp.legacyProtocolNotice', {
          version: e.compatibility_notice.protocol_version || e.protocol_version || '?'
        })) + '</span></div>';
    }
    // Footer: repo link (left) + tools count / action buttons (right)
    html += '<div class="mcp-app-footer">';
    if (e.url) {
      html += '<a class="mcp-app-repo" href="' + escapeHtml(e.url) + '" target="_blank" rel="noopener" title="' + escapeHtml(t('mcp.repoTitle')) + '">' + REPO_SVG + ' ' + escapeHtml(t('mcp.repo')) + '</a>';
    } else {
      html += '<span></span>';
    }
    html += '<div class="mcp-app-action">';
    if (pending) {
      html += '<span class="mcp-app-pending-note">' + escapeHtml(
        _mcpPendingLabel(pending)) + '</span>';
    } else if (connected) {
      if (credExpired) {
        // The subprocess is live but the stored cookie/token no longer
        // authenticates. Offer a one-click path to re-enter credentials —
        // the same reinstall modal used for editing an installed server
        // (existing env prefills, blank leaves it unchanged).
        html += '<button class="btn btn-primary btn-xs" data-tofu-action="_mcpOpenInstallModal(\'' + escapeHtml(e.id) + '\', true)" title="' + escapeHtml(t('mcp.credExpiredTitle')) + '">' + escapeHtml(t('mcp.updateCreds')) + '</button>';
      }
      if (e.server_version) {
        html += '<span class="mcp-app-version" title="' + escapeHtml((e.server_impl_name || e.id) + ' v' + e.server_version) + '">v' + escapeHtml(e.server_version) + '</span>';
      }
      if (e.tools_count) {
        var _disabledCount = (e.disabled_tools || []).length;
        var _countLabel = _disabledCount > 0
          ? t('mcp.toolsCountOf', { a: (e.tools_count - _disabledCount), b: e.tools_count })
          : t('mcp.toolsCount', { n: (e.tools_count || 0) });
        html += '<button class="mcp-app-tools-count mcp-tools-toggle" data-tofu-action="_mcpToggleToolsPanel(\'' + escapeHtml(e.id) + '\')" title="' + escapeHtml(t('mcp.toolsToggleTitle')) + '">' +
          (_mcpToolsOpen[e.id] ? '▾ ' : '▸ ') + escapeHtml(_countLabel) + '</button>';
      }
      html += _mcpUpdateButtonHtml(e);
      html += '<button class="btn btn-secondary btn-xs" data-tofu-action="_mcpUninstall(\'' + escapeHtml(e.id) + '\')" title="' + escapeHtml(t('mcp.uninstallTitle')) + '">' + escapeHtml(t('mcp.uninstall')) + '</button>';
    } else if (installed) {
      if (breaker) {
        html += '<span class="mcp-app-reconnecting-note" title="' +
          escapeHtml(t('mcp.reconnecting')) + '">⟳ ' +
          _mcpBreakerCountdownSpan(breaker) + '</span>';
      }
      if (e.custom) {
        // Custom servers have no catalog entry, so the catalog install
        // endpoint would 404. Reconnect straight through connectOne.
        html += '<button class="btn btn-primary btn-xs" data-tofu-action="_mcpReconnect(\'' + escapeHtml(e.id) + '\')" title="' + escapeHtml(t('mcp.connectCustomTitle')) + '">' + escapeHtml(t('mcp.connect')) + '</button>';
      } else {
        html += '<button class="btn btn-primary btn-xs" data-tofu-action="_mcpOpenInstallModal(\'' + escapeHtml(e.id) + '\', true)" title="' + escapeHtml(t('mcp.connectReinstallTitle')) + '">' + escapeHtml(t('mcp.connect')) + '</button>';
      }
      html += _mcpUpdateButtonHtml(e);
      html += '<button class="btn btn-secondary btn-xs" data-tofu-action="_mcpPurge(\'' + escapeHtml(e.id) + '\')" title="' + escapeHtml(t('mcp.purgeTitle')) + '">' + escapeHtml(t('mcp.purge')) + '</button>';
    } else {
      // If the catalog entry has NO required env vars, skip the modal
      // entirely and one-click-install with the built-in defaults. The
      // modal is only useful when the user must fill something in
      // (API keys, tokens, etc.) — showing a form full of optional
      // "PATH TO EXECUTABLE / TIMEOUT / MAX CONCURRENCY" tweaks for
      // servers like Hope just creates confusion.
      var _needsInput = (e.env_specs || []).some(function(s) { return s.required; });
      if (_needsInput) {
        html += '<button class="btn btn-primary btn-xs" data-tofu-action="_mcpOpenInstallModal(\'' + escapeHtml(e.id) + '\')">' + escapeHtml(t('mcp.install')) + '</button>';
      } else {
        html += '<button class="btn btn-primary btn-xs" data-tofu-action="_mcpQuickInstall(\'' + escapeHtml(e.id) + '\')" title="' + escapeHtml(t('mcp.quickInstallTitle')) + '">' + escapeHtml(t('mcp.install')) + '</button>';
      }
    }
    html += '</div></div>';  // action + footer
    if (connected && !pending && _mcpToolsOpen[e.id]) {
      html += _renderMcpToolPanel(e);
    }
    html += '</div>';  // card
  });
  grid.innerHTML = html;
  _mcpScheduleBreakerRefresh();
}

/* ═══════════════════════════════════════════════════════════════════
   Per-tool toggles ()
   A connected card's tools badge expands into a checkbox list — one row
   per upstream tool, checked = offered to the model. Toggling saves the
   FULL disabled list immediately (full-replacement PUT semantics), with
   optimistic UI and revert-on-failure.
   ═══════════════════════════════════════════════════════════════════ */

function _mcpToggleToolsPanel(serverId) {
  if (_mcpToolsOpen[serverId]) {
    delete _mcpToolsOpen[serverId];
  } else {
    _mcpToolsOpen[serverId] = true;
    if (!_mcpToolsCache[serverId]) _mcpLoadTools(serverId);
  }
  _renderMcpCatalog();
}

async function _mcpLoadTools(serverId) {
  try {
    var r = await Api.mcp.toolsListForServer(serverId);
    if (!r || !r.ok) throw new Error('HTTP ' + (r ? r.status : 'no response'));
    var data = await r.json();
    _mcpToolsCache[serverId] = data.tools || [];
  } catch (e) {
    debugLog('[MCP] Failed to load tools for ' + serverId + ': ' + e.message, 'error');
    _mcpToolsCache[serverId] = [];
  }
  if (_mcpToolsOpen[serverId]) _renderMcpCatalog();
}

function _renderMcpToolPanel(e) {
  var rows = _mcpToolsCache[e.id];
  var html = '<div class="mcp-tool-panel">';
  if (!rows) {
    html += '<div class="mcp-tool-loading">' + escapeHtml(t('mcp.loading')) + '</div>';
  } else if (rows.length === 0) {
    html += '<div class="mcp-tool-loading">—</div>';
  } else {
    var enabledCount = rows.filter(function(x) { return x.enabled; }).length;
    /* Header is a pinned toolbar: live enabled/total count on the left,
     * bulk enable/disable on the right; only the tool grid below scrolls,
     * so the count never scrolls away on long lists. */
    html += '<div class="mcp-tool-panel-head">' +
      '<span class="mcp-tool-panel-count">' +
      escapeHtml(t('mcp.toolsEnabledOf', { a: enabledCount, b: rows.length })) + '</span>' +
      '<span class="mcp-tool-bulk">' +
      '<button type="button" class="mcp-tool-bulk-btn" data-tofu-action="_mcpSetAllTools(\'' + escapeHtml(e.id) + '\',true)">' + escapeHtml(t('mcp.toolsEnableAll')) + '</button>' +
      '<button type="button" class="mcp-tool-bulk-btn" data-tofu-action="_mcpSetAllTools(\'' + escapeHtml(e.id) + '\',false)">' + escapeHtml(t('mcp.toolsDisableAll')) + '</button>' +
      '</span></div>';
    html += '<div class="mcp-tool-grid">';
    rows.forEach(function(x) {
      /* The visible control is the house .stg-toggle (visually-hidden input
       * + span track/thumb): ambient modal/panel input resets (.modal input,
       * [data-theme] .modal:not(...) input:not(...)) paint the 1px hidden
       * input and cannot deform the switch. */
      html += '<label class="mcp-tool-row" title="' + escapeHtml(x.description || '') + '">' +
        '<span class="mcp-tool-name">' + escapeHtml(x.name) + '</span>' +
        '<span class="stg-toggle">' +
        '<input type="checkbox" ' + (x.enabled ? 'checked ' : '') +
        'data-tofu-action-change="_mcpToggleTool(\'' + escapeHtml(e.id) + '\',\'' + escapeHtml(x.name) + '\',this.checked)">' +
        '<span class="stg-toggle-track"><span class="stg-toggle-thumb"></span></span>' +
        '</span></label>';
    });
    html += '</div>';
  }
  html += '</div>';
  return html;
}

/* Shared persist path for per-tool toggles and bulk enable/disable:
 * the mutation has already been applied optimistically to the cache; this
 * PUTs the FULL disabled list (full-replacement semantics) and rolls back
 * via the caller-supplied undo closure on failure. */
async function _mcpSaveTools(serverId, undo) {
  var rows = _mcpToolsCache[serverId];
  var disabled = rows.filter(function(x) { return !x.enabled; })
    .map(function(x) { return x.name; });
  try {
    var r = await Api.mcp.serverToolsSet(serverId, disabled);
    if (!r || !r.ok) throw new Error('HTTP ' + (r ? r.status : 'no response'));
    var data = typeof r.json === 'function'
      ? await r.json().catch(function() { return {}; }) : {};
    var entry = _mcpCatalog.filter(function(x) { return x.id === serverId; })[0];
    if (entry) entry.disabled_tools = disabled;
    if (data.mcp_tool_summary
        && typeof runtimeScope.applyMcpToolSummary === 'function') {
      runtimeScope.applyMcpToolSummary(data.mcp_tool_summary);
    }
    _renderMcpCatalog();
    _mcpUpdateToolCount();
  } catch (err) {
    undo();
    showAlert(t('mcp.toolsToggleFailed', { err: err.message }));
    _renderMcpCatalog();
  }
}

async function _mcpToggleTool(serverId, toolName, enabled) {
  var rows = _mcpToolsCache[serverId];
  if (!rows) return;
  // Optimistic update; revert on failure.
  rows.forEach(function(x) { if (x.name === toolName) x.enabled = enabled; });
  debugLog('[MCP] ' + serverId + ': ' + (enabled ? 'enabled' : 'disabled') + ' tool ' + toolName, 'success');
  await _mcpSaveTools(serverId, function() {
    rows.forEach(function(x) { if (x.name === toolName) x.enabled = !enabled; });
  });
}

async function _mcpSetAllTools(serverId, enabled) {
  var rows = _mcpToolsCache[serverId];
  if (!rows) return;
  var prev = rows.map(function(x) { return x.enabled; });
  rows.forEach(function(x) { x.enabled = enabled; });
  debugLog('[MCP] ' + serverId + ': ' + (enabled ? 'enabled' : 'disabled') + ' ALL tools', 'success');
  await _mcpSaveTools(serverId, function() {
    rows.forEach(function(x, i) { x.enabled = prev[i]; });
  });
}

/**
 * While any installed-but-disconnected server has an active circuit
 * breaker, schedule a single re-fetch so the "retry in N" countdown stays
 * fresh and the card flips to ON automatically once auto-reconnect
 * succeeds. Self-cancelling: re-poll cadence is capped at 15s and the
 * timer stops as soon as no breaker remains or the grid leaves the DOM
 * (settings panel closed / tab switched).
 */
function _mcpScheduleBreakerRefresh() {
  if (_mcpBreakerRefreshTimer) {
    clearTimeout(_mcpBreakerRefreshTimer);
    _mcpBreakerRefreshTimer = null;
  }
  var active = _mcpCatalog.filter(function(e) {
    return e.installed && !e.connected && e.breaker;
  });
  if (active.length === 0) { _mcpStopBreakerTick(); return; }

  // Re-poll a bit after the soonest retry is due (so the next fetch sees
  // the post-attempt state), clamped to [3s, 15s] to avoid hammering.
  var soonest = Math.min.apply(null, active.map(function(e) {
    return Math.max(0, e.breaker.retry_in || 0);
  }));
  var delayMs = Math.min(15000, Math.max(3000, (soonest + 1) * 1000));

  _mcpBreakerRefreshTimer = setTimeout(function() {
    _mcpBreakerRefreshTimer = null;
    var grid = document.getElementById('mcpCatalogGrid');
    // Bail if the MCP tab is no longer visible — no point polling a
    // detached / hidden grid.
    if (!grid || !grid.isConnected || grid.offsetParent === null) return;
    _populateMcpTab();
  }, delayMs);

  // Start the per-second countdown ticker so the "retry in N" text
  // decrements smoothly between server re-polls (a frozen number looks
  // broken). The ticker only touches the small countdown spans, never
  // re-renders the grid.
  _mcpStartBreakerTick();
}

/**
 * Update every live breaker-countdown span from its ``data-retry-at``
 * deadline. Runs once per second. Self-stops when no spans remain or the
 * grid is no longer visible (settings closed / tab switched), so it never
 * leaks a timer.
 */
function _mcpTickBreakers() {
  var grid = document.getElementById('mcpCatalogGrid');
  if (!grid || !grid.isConnected || grid.offsetParent === null) {
    _mcpStopBreakerTick();
    return;
  }
  var spans = grid.querySelectorAll('.mcp-breaker-countdown');
  if (spans.length === 0) {
    _mcpStopBreakerTick();
    return;
  }
  var now = Date.now();
  for (var i = 0; i < spans.length; i++) {
    var deadline = parseInt(spans[i].getAttribute('data-retry-at'), 10) || 0;
    var label = _mcpRetryLabel((deadline - now) / 1000);
    if (spans[i].textContent !== label) spans[i].textContent = label;
  }
}

function _mcpStartBreakerTick() {
  if (_mcpBreakerTickTimer) return;  // already ticking
  _mcpBreakerTickTimer = setInterval(_mcpTickBreakers, 1000);
}

function _mcpStopBreakerTick() {
  if (_mcpBreakerTickTimer) {
    clearInterval(_mcpBreakerTickTimer);
    _mcpBreakerTickTimer = null;
  }
}

/** Update "installed" badge and "connect all" button in the header. */
function _renderMcpInstalled() {
  var countEl = document.getElementById('mcpInstalledCount');
  var connectAllBtn = document.getElementById('mcpConnectAllBtn');

  var connectedApps = _mcpCatalog.filter(function(e) { return e.connected; });
  var installedNotConnected = _mcpCatalog.filter(function(e) { return e.installed && !e.connected; });
  var total = connectedApps.length + installedNotConnected.length;

  if (countEl) {
    if (total > 0) {
      countEl.textContent = t('mcp.installedCount', { n: total });
      countEl.style.display = '';
    } else {
      countEl.style.display = 'none';
    }
  }
  if (connectAllBtn) {
    connectAllBtn.style.display = installedNotConnected.length > 0 ? '' : 'none';
  }
}

// ── Install Modal ──

/**
 * One-click install: POST /api/mcp/catalog/install with an empty env so
 * the backend uses every env_spec's default. Intended for catalog entries
 * that have zero `required: true` env_specs (e.g. Hope, where all four
 * fields — HOPE_BIN, HOPE_MCP_TIMEOUT, HOPE_MCP_MAX_PARALLEL,
 * HOPE_MCP_DRY_RUN_DEFAULT — have sensible built-in defaults). Skips the
 * modal entirely. Users who want to tweak the defaults can still do so
 * by clicking the "连接" (Reconnect) button after installation, which
 * opens the same modal pre-filled.
 *
 * NOTE: We intentionally do NOT show a confirmation dialog — the whole
 * point is that a one-click install should feel instant. Status is
 * surfaced via the grid's own connect/disconnect animation and the
 * app-level debugLog, same as the "Connect All" button.
 */
/**
 * Extract a human-readable failure reason from an error thrown by the
 * Api layer. catalogInstall now lets HTTP 500s throw an ApiError whose
 * `.body` carries the backend's rich `{error, stderr_tail}` payload —
 * we prefer that over the generic "HTTP 500 on ..." message so the user
 * sees the actual connection failure (e.g. a launcher traceback tail).
 */
function _mcpErrDetail(e) {
  var body = e && e.body;
  if (body && typeof body === 'object') {
    var msg = body.error || e.message || t('mcp.unknownError');
    if (body.stderr_tail) msg += '\n\n' + t('mcp.serverOutputLabel') + '\n' + body.stderr_tail;
    return msg;
  }
  return (e && e.message) || t('mcp.unknownErrorNoConn');
}

async function _mcpQuickInstall(serverId) {
  var entry = _mcpCatalog.find(function(e) { return e.id === serverId; });
  if (!entry) return;
  /* INSTANT-UI (): the card shows 安装中… on the CLICK
   *   frame — the old code only wrote to the debug panel and the card sat
   *   static until the install + poll finished. */
  _mcpSetPending(serverId, 'installing');
  debugLog('[MCP] Quick-installing ' + serverId + ' (no required env)…', 'info');
  try {
    var data = await Api.mcp.catalogInstall(serverId, {});
    if (data && data.ok && data.status === 'installing') {
      debugLog('[MCP] ' + serverId + ' installing deps in background; polling…', 'info');
      data = await _mcpPollInstall(serverId, null);
    }
    if (data && data.ok && data.status !== 'error') {
      debugLog('[MCP] Installed ' + serverId + ': ' + (data.tools_count || 0) + ' tools', 'success');
      _mcpClearPending(serverId, true);   // repopulate renders the fresh state
      await _populateMcpTab();
    } else {
      // Installation failed — fall back to opening the modal so the user
      // can inspect the default values and/or override them. This is the
      // safety net for "hope binary not on PATH" kinds of errors.
      _mcpClearPending(serverId);         // restore the card first
      var _err = (data && data.error) || t('mcp.unknownErrorNoConn');
      debugLog('[MCP] Quick install failed (' + _err + '); opening install modal for ' + serverId, 'warning');
      showAlert(t('mcp.quickInstallFailed', { err: _err }));
      _mcpOpenInstallModal(serverId);
    }
  } catch (e) {
    _mcpClearPending(serverId);           // restore the card first
    var _detail = _mcpErrDetail(e);
    debugLog('[MCP] Quick install error for ' + serverId + ': ' + _detail, 'error');
    showAlert(t('mcp.quickInstallFailed', { err: _detail }));
    _mcpOpenInstallModal(serverId);
  }
}

/**
 * The placeholder for a credential input.
 *
 * Always the spec's own hint when it has one. Previously this returned the
 * "already saved, leave blank to keep" notice INSTEAD of the hint whenever a
 * value was stored — which silently removed the guidance at the one moment a
 * user is most likely to need it (rotating an expired key). The saved notice
 * is now rendered as its own line by the caller, so neither fact evicts the
 * other.
 */
function _mcpPlaceholder(spec, hasStored) {
  if (spec.hint) return spec.hint;
  return hasStored ? t('mcp.savedHint') : '';
}

/**
 * Render "where do I get this credential" as a real link (+ optional ordered
 * steps), instead of a breadcrumb crammed into the placeholder.
 *
 * A placeholder cannot be clicked, is truncated by the input width, and
 * disappears the moment the user types — so a console path written there made
 * the user hand-transcribe a URL. Returns '' when the entry declares no
 * route, so nothing is invented for servers that need no credential.
 */
function _mcpObtainBlock(spec) {
  var url = spec.obtain_url || '';
  var steps = Array.isArray(spec.obtain_steps) ? spec.obtain_steps : [];
  if (!url && steps.length === 0) return '';
  var html = '<div class="mcp-obtain">';
  if (url) {
    // Only http(s) — a catalog entry is server-owned, but this is the one
    // place a URL becomes a clickable target, so reject anything that could
    // carry a javascript: payload.
    var safe = /^https?:\/\//i.test(url) ? url : '';
    if (safe) {
      html += '<a class="mcp-obtain-link" href="' + escapeHtml(safe) +
        '" target="_blank" rel="noopener noreferrer">' +
        escapeHtml(t('mcp.obtainKey')) + ' ↗</a>';
    }
  }
  if (steps.length > 0) {
    html += '<ol class="mcp-obtain-steps">';
    steps.forEach(function(s) {
      html += '<li>' + escapeHtml(String(s)) + '</li>';
    });
    html += '</ol>';
  }
  html += '</div>';
  return html;
}

/**
 * Catalog entries that ALREADY have a stored value for `key`, excluding
 * `selfId`. Two cards can legitimately share one credential (RollingGo hotel
 * + flight share ROLLINGGO_API_KEY; github + github-batch share a PAT), and
 * without this the second install asks for a key the user already gave us —
 * so they go re-apply for a duplicate.
 */
function _mcpSharedCredentialSources(key, selfId) {
  return _mcpCatalog.filter(function(e) {
    return e.id !== selfId
      && (e.stored_env_keys || []).indexOf(key) !== -1;
  });
}

function _mcpOpenInstallModal(serverId, isReinstall) {
  var entry = _mcpCatalog.find(function(e) { return e.id === serverId; });
  if (!entry) return;
  _mcpInstallTarget = entry;
  _mcpInstallIsReinstall = !!isReinstall;

  // Icon may be either an emoji (e.g. '🐙') OR an inline SVG string (e.g.
  // '<svg viewBox="0 0 24 24">…</svg>' for brand logos like Meituan/Hope).
  // Using .textContent would render the SVG source as literal text, which
  // is the "path d=…" garble users saw when opening brand-icon apps.
  // Catalog icons are server-owned (lib/mcp/registry.py), not user input,
  // so innerHTML is safe here — matches how _renderMcpCatalog() already
  // emits the same icon strings into the grid cards on L3609.
  var _icon = entry.icon || Icon('plug', 34);
  document.getElementById('mcpInstallIcon').innerHTML =
    (typeof _icon === 'string' && _icon.trim().startsWith('<'))
      ? _icon
      : escapeHtml(_icon);
  document.getElementById('mcpInstallTitle').textContent = entry.name;
  document.getElementById('mcpInstallDesc').textContent = entry.description || '';
  var noteEl = document.getElementById('mcpInstallNote');
  if (noteEl) {
    if (entry.install_note) {
      noteEl.textContent = entry.install_note;
      noteEl.style.display = '';
    } else {
      noteEl.textContent = '';
      noteEl.style.display = 'none';
    }
  }
  var repoLink = document.getElementById('mcpInstallRepo');
  if (repoLink) {
    if (entry.url) {
      repoLink.href = entry.url;
      repoLink.innerHTML = '<svg width="9" height="9" viewBox="0 0 16 16" fill="currentColor"><path d="M8 0C3.58 0 0 3.58 0 8c0 3.54 2.29 6.53 5.47 7.59.4.07.55-.17.55-.38 0-.19-.01-.82-.01-1.49-2.01.37-2.53-.49-2.69-.94-.09-.23-.48-.94-.82-1.13-.28-.15-.68-.52-.01-.53.63-.01 1.08.58 1.23.82.72 1.21 1.87.87 2.33.66.07-.52.28-.87.51-1.07-1.78-.2-3.64-.89-3.64-3.95 0-.87.31-1.59.82-2.15-.08-.2-.36-1.02.08-2.12 0 0 .67-.21 2.2.82.64-.18 1.32-.27 2-.27.68 0 1.36.09 2 .27 1.53-1.04 2.2-.82 2.2-.82.44 1.1.16 1.92.08 2.12.51.56.82 1.27.82 2.15 0 3.07-1.87 3.75-3.65 3.95.29.25.54.73.54 1.48 0 1.07-.01 1.93-.01 2.2 0 .21.15.46.55.38A8.013 8.013 0 0016 8c0-4.42-3.58-8-8-8z"/></svg>' + escapeHtml(entry.url.replace(/^https?:\/\//, ''));
      repoLink.style.display = '';
    } else {
      repoLink.style.display = 'none';
    }
  }
  document.getElementById('mcpInstallStatus').style.display = 'none';

  // Build env fields. Split into required + optional so the modal shows
  // only what the user MUST configure by default, and hides the
  // advanced knobs (timeouts, paths, …) behind a <details> toggle. For
  // Hope this reduces the visible form from 4 confusing fields to one
  // clear "username" prompt.
  var fieldsHtml = '';
  var specs = entry.env_specs || [];
  var storedKeys = (entry.stored_env_keys || []);

  function _renderSpec(spec) {
    var hasStored = storedKeys.indexOf(spec.key) !== -1;
    if (spec.type === 'select' && Array.isArray(spec.options)) {
      return _renderSelectSpec(spec, hasStored);
    }
    var inputType = (spec.secret !== false) ? 'password' : 'text';
    var html = '<div class="stg-field">';
    html += '<label>' + escapeHtml(spec.label || spec.key);
    if (spec.required) html += ' <span style="color:#ef4444;">*</span>';
    if (hasStored) html += ' <span style="color:#10b981;font-size:11px;">' + escapeHtml(t('mcp.savedBadge')) + '</span>';
    html += '</label>';
    html += _mcpObtainBlock(spec);
    html += '<input type="' + inputType + '" class="mcp-env-input" data-key="' + escapeHtml(spec.key) + '" data-has-stored="' + (hasStored ? '1' : '0') + '" placeholder="' + escapeHtml(_mcpPlaceholder(spec, hasStored)) + '">';
    if (hasStored) {
      // The "leave blank to keep" notice used to be pushed into the
      // PLACEHOLDER, which meant it REPLACED spec.hint — so on a reinstall
      // (exactly when someone is rotating a credential) the guidance about
      // what to type disappeared. Both facts matter, so both are shown.
      html += '<span class="stg-hint mcp-env-saved-note">' + escapeHtml(t('mcp.savedHint')) + '</span>';
    } else {
      // Not stored for THIS entry — but a sibling entry may already hold the
      // very same credential. Say so, otherwise the user re-applies for a key
      // they already have.
      var shared = _mcpSharedCredentialSources(
        spec.key, entry && entry.id);
      if (shared.length > 0) {
        html += '<span class="stg-hint mcp-env-shared-note">' +
          escapeHtml(t('mcp.sharedCredential', {
            name: shared.map(function(s) { return s.name || s.id; }).join('、'),
          })) + '</span>';
      }
    }
    html += '</div>';
    return html;
  }

  // A select-type spec renders a provider dropdown that drives a companion
  // (normally hidden) text input carrying the real env value. Picking a known
  // provider fills the host AND any sibling fields named in the option's
  // `autofill` map (e.g. SMTP host + ports). The "__custom__" option reveals
  // the text input for manual entry. The dropdown itself is class
  // `mcp-env-select` (NOT `mcp-env-input`), so it is never collected as a
  // value at install time — only the companion input is.
  function _renderSelectSpec(spec, hasStored) {
    var html = '<div class="stg-field">';
    html += '<label>' + escapeHtml(spec.label || spec.key);
    if (spec.required) html += ' <span style="color:#ef4444;">*</span>';
    if (hasStored) html += ' <span style="color:#10b981;font-size:11px;">' + escapeHtml(t('mcp.savedBadge')) + '</span>';
    html += '</label>';
    html += '<select class="mcp-env-select" data-tofu-dd data-select-for="' + escapeHtml(spec.key) + '" data-tofu-action-change="_mcpEnvPresetChanged(this)">';
    html += '<option value="">' + escapeHtml(t('mcp.selectProvider')) + '</option>';
    spec.options.forEach(function(opt) {
      var af = opt.autofill ? escapeHtml(JSON.stringify(opt.autofill)) : '';
      html += '<option value="' + escapeHtml(opt.value) + '" data-autofill="' + af + '">' + escapeHtml(opt.label) + '</option>';
    });
    html += '</select>';
    html += _mcpObtainBlock(spec);
    html += '<input type="text" class="mcp-env-input" data-key="' + escapeHtml(spec.key) + '" data-has-stored="' + (hasStored ? '1' : '0') + '" placeholder="' + escapeHtml(_mcpPlaceholder(spec, hasStored)) + '" style="margin-top:6px;display:none;">';
    if (hasStored) {
      html += '<span class="stg-hint mcp-env-saved-note">' + escapeHtml(t('mcp.savedHint')) + '</span>';
    }
    html += '</div>';
    return html;
  }

  if (specs.length === 0) {
    fieldsHtml = '<p class="mcp-install-noenv">' + escapeHtml(t('mcp.noEnvNeeded')) + '</p>';
  } else {
    var required = specs.filter(function(s) { return s.required; });
    var optional = specs.filter(function(s) { return !s.required; });
    required.forEach(function(spec) { fieldsHtml += _renderSpec(spec); });
    if (optional.length > 0) {
      fieldsHtml += '<details class="mcp-advanced-toggle" style="margin-top:12px;">';
      fieldsHtml += '<summary style="cursor:pointer;color:var(--text-muted);font-size:12px;user-select:none;">' + escapeHtml(t('mcp.advancedToggle', { n: optional.length })) + '</summary>';
      fieldsHtml += '<div style="margin-top:8px;">';
      optional.forEach(function(spec) { fieldsHtml += _renderSpec(spec); });
      fieldsHtml += '</div></details>';
    }
  }
  document.getElementById('mcpInstallFields').innerHTML = fieldsHtml;
  if (runtimeScope.TofuDropdown) runtimeScope.TofuDropdown.enhance(document.getElementById('mcpInstallFields'));

  var btn = document.getElementById('mcpInstallBtn');
  btn.disabled = false;
  btn.textContent = _mcpInstallIsReinstall ? t('mcp.saveConnect') : t('mcp.installConnect');

  document.getElementById('mcpInstallOverlay').style.display = 'flex';
}

/**
 * Provider-dropdown change handler. Drives the companion host input for the
 * select's own key, and applies the chosen option's `autofill` map to sibling
 * env inputs (e.g. SMTP host + ports). Choosing "__custom__" clears and
 * reveals the host input for manual entry; choosing the blank prompt hides it.
 */
function _mcpEnvPresetChanged(sel) {
  var fields = document.getElementById('mcpInstallFields');
  if (!fields) return;
  var key = sel.getAttribute('data-select-for');
  var hostInput = fields.querySelector('.mcp-env-input[data-key="' + key + '"]');
  var opt = sel.options[sel.selectedIndex];
  var value = sel.value;

  if (!value) {
    if (hostInput) { hostInput.value = ''; hostInput.style.display = 'none'; }
    return;
  }
  if (value === '__custom__') {
    if (hostInput) { hostInput.value = ''; hostInput.style.display = ''; hostInput.focus(); }
    return;
  }
  // Known provider: set host, keep the input hidden (value still collected).
  if (hostInput) { hostInput.value = value; hostInput.style.display = 'none'; }

  var afRaw = opt ? opt.getAttribute('data-autofill') : '';
  if (afRaw) {
    var autofill = {};
    try { autofill = JSON.parse(afRaw); }
    catch (e) { debugLog('[MCP] bad autofill JSON: ' + e.message, 'warning'); }
    Object.keys(autofill).forEach(function(k) {
      var sib = fields.querySelector('.mcp-env-input[data-key="' + k + '"]');
      if (sib) sib.value = autofill[k];
    });
  }
}

function _mcpCloseInstallModal(evt) {
  if (evt && evt.target !== evt.currentTarget) return;
  document.getElementById('mcpInstallOverlay').style.display = 'none';
  _mcpInstallTarget = null;
}

/**
 * Poll an async catalog install until it finishes (ready / error).
 * Returns the final parsed body ({ok, status, tools_count, error, stderr_tail}).
 * Bounded so a stuck install eventually surfaces an error rather than spinning
 * forever (cold pip is minutes, not hours).
 */
async function _mcpPollInstall(serverId, statusEl) {
  var DEADLINE_MS = 6 * 60 * 1000;   // 6 min hard cap
  var INTERVAL_MS = 2500;
  var t0 = Date.now();
  while (Date.now() - t0 < DEADLINE_MS) {
    await new Promise(function(r) { setTimeout(r, INTERVAL_MS); });
    if (statusEl) {
      var secs = Math.round((Date.now() - t0) / 1000);
      statusEl.textContent = t('mcp.installingDeps', { n: secs });
    }
    var resp = await Api.mcp.catalogInstallStatus(serverId);
    if (!resp) continue;                       // transient network blip — retry
    var body = await resp.json().catch(function() { return null; });
    if (!body) continue;
    if (body.status === 'installing') continue;
    // ready / error / unknown → return for the caller to render.
    return body;
  }
  return { ok: false, status: 'error', error: t('mcp.installTimeout') };
}

async function _mcpDoInstall() {
  if (!_mcpInstallTarget) return;
  var btn = document.getElementById('mcpInstallBtn');
  var status = document.getElementById('mcpInstallStatus');
  btn.disabled = true;
  btn.textContent = t('mcp.installing');
  status.style.display = 'block';
  status.className = 'mcp-install-status info';
  status.textContent = t('mcp.startingFirstInstall', { name: _mcpInstallTarget.name });

  // Collect env values
  var env = {};
  var inputs = document.querySelectorAll('#mcpInstallFields .mcp-env-input');
  for (var i = 0; i < inputs.length; i++) {
    var key = inputs[i].getAttribute('data-key');
    var val = inputs[i].value.trim();
    if (val) env[key] = val;
  }

  try {
    var data = await Api.mcp.catalogInstall(_mcpInstallTarget.id, env);
    // Async path: server kicked off a background pip install. Poll status.
    if (data && data.ok && data.status === 'installing') {
      status.textContent = t('mcp.installingDepsName', { name: _mcpInstallTarget.name });
      data = await _mcpPollInstall(_mcpInstallTarget.id, status);
    }
    if (data && data.ok && data.status !== 'error') {
      status.className = 'mcp-install-status success';
      status.textContent = t('mcp.installedSuccess', { name: _mcpInstallTarget.name, n: (data.tools_count || 0) });
      debugLog('[MCP] Installed ' + _mcpInstallTarget.id + ': ' + (data.tools_count || 0) + ' tools', 'success');
      // Refresh catalog after a short delay so user sees the success
      setTimeout(function() {
        _mcpCloseInstallModal();
        _populateMcpTab();
      }, 1200);
    } else {
      var _msg = (data && data.error) || t('mcp.installFailedNoConn');
      if (data && data.stderr_tail) _msg += '\n\n' + t('mcp.serverOutputLabel') + '\n' + data.stderr_tail;
      status.className = 'mcp-install-status error';
      status.textContent = '✕ ' + _msg;
      btn.disabled = false;
      btn.textContent = t('mcp.retry');
    }
  } catch (e) {
    status.className = 'mcp-install-status error';
    status.textContent = '✕ ' + _mcpErrDetail(e);
    btn.disabled = false;
    btn.textContent = t('mcp.retry');
  }
}

// ── Uninstall / Reconnect ──

// Soft uninstall: disconnect + disable, but keep credentials for easy re-enable.
async function _mcpUninstall(serverId) {
  var entry = _mcpCatalog.find(function(e) { return e.id === serverId; });
  var name = entry ? entry.name : serverId;
  if (!await showConfirm(t('mcp.uninstallConfirm', { name: name }))) return;

  /* INSTANT-UI (): the card shows 卸载中… the moment
   *   the confirm closes — the old code sat static for the whole DELETE RTT. */
  _mcpSetPending(serverId, 'uninstalling');
  try {
    var data = await Api.mcp.catalogUninstall(serverId, false);
    if (!data || !data.ok) { _mcpClearPending(serverId); showAlert(t('mcp.uninstallFailed', { err: ((data && data.error) || t('mcp.unknownError')) })); return; }
    debugLog('[MCP] Uninstalled ' + serverId + (data.purged ? ' (purged)' : ' (soft, env kept)'), 'info');
    _mcpClearPending(serverId, true);     // repopulate renders the fresh state
    await _populateMcpTab();
  } catch (e) {
    _mcpClearPending(serverId);
    showAlert(t('mcp.uninstallFailed', { err: e.message }));
  }
}

// Hard purge: remove config row entirely, forgetting stored credentials.
async function _mcpPurge(serverId) {
  var entry = _mcpCatalog.find(function(e) { return e.id === serverId; });
  var name = entry ? entry.name : serverId;
  if (!await showConfirm(t('mcp.purgeConfirm', { name: name }), { danger: true })) return;

  /* INSTANT-UI (): same pending shape as uninstall. */
  _mcpSetPending(serverId, 'uninstalling');
  try {
    var data = await Api.mcp.catalogUninstall(serverId, true);
    if (!data || !data.ok) { _mcpClearPending(serverId); showAlert(t('mcp.purgeFailed', { err: ((data && data.error) || t('mcp.unknownError')) })); return; }
    debugLog('[MCP] Purged ' + serverId, 'info');
    _mcpClearPending(serverId, true);     // repopulate renders the fresh state
    await _populateMcpTab();
  } catch (e) {
    _mcpClearPending(serverId);
    showAlert(t('mcp.purgeFailed', { err: e.message }));
  }
}

async function _mcpConnectAll() {
  var btn = document.getElementById('mcpConnectAllBtn');
  if (btn) { btn.disabled = true; btn.textContent = t('mcp.connecting'); }
  try {
    var data = await Api.mcp.connectAll();
    if (!data || !data.ok) { showAlert(t('mcp.connectFailed', { err: ((data && data.error) || t('mcp.unknownError')) })); return; }
    var total = data.total_tools || 0;
    var count = Object.keys(data.servers || {}).length;
    debugLog('[MCP] Connected all: ' + count + ' server(s), ' + total + ' tools', 'success');
    await _populateMcpTab();
  } catch (e) {
    showAlert(t('mcp.connectFailed', { err: e.message }));
  } finally {
    if (btn) { btn.disabled = false; btn.textContent = t('mcp.connectAll'); }
  }
}

async function _mcpReconnect(serverId) {
  /* INSTANT-UI (): the card shows 连接中… on the CLICK
   *   frame — MCP cold start is measured at 27-55s (JOURNAL), the longest
   *   dead window in the app. */
  _mcpSetPending(serverId, 'connecting');
  try {
    var data = await Api.mcp.connectOne(serverId);
    if (!data || !data.ok) { _mcpClearPending(serverId); showAlert(t('mcp.connectFailed', { err: ((data && data.error) || t('mcp.unknownError')) })); return; }
    debugLog('[MCP] Reconnected ' + serverId + ': ' + (data.tools_count || 0) + ' tools', 'success');
    _mcpClearPending(serverId, true);     // repopulate renders the fresh state
    await _populateMcpTab();
  } catch (e) {
    _mcpClearPending(serverId);
    showAlert(t('mcp.connectFailed', { err: e.message }));
  }
}


/**
 * Update button for an installed card whose upstream registry has a newer
 * release than the stored launch spec would start. `_mcpUpdates` fills in
 * after the grid paints, so the button appears on the check's re-render.
 */
function _mcpUpdateButtonHtml(e) {
  var upd = _mcpUpdates[e.id];
  if (!upd || !upd.update_available || !upd.latest) return '';
  return '<button class="btn btn-primary btn-xs" data-tofu-action="_mcpApplyUpdate(\'' +
    escapeHtml(e.id) + '\')" title="' +
    escapeHtml(t('mcp.updateTitle', { current: upd.current || '?', latest: upd.latest })) + '">' +
    escapeHtml(t('mcp.updateTo', { version: upd.latest })) + '</button>';
}

/**
 * One-click upstream update: the backend rewrites the stored launch args to
 * pin the registry-latest release (credentials preserved) and reconnects,
 * so the new code is what actually runs. Same instant-pending shape as
 * reconnect/uninstall ().
 */
async function _mcpApplyUpdate(serverId) {
  _mcpSetPending(serverId, 'updating');
  try {
    var data = await Api.mcp.updateApply(serverId);
    if (!data || !data.ok) {
      _mcpClearPending(serverId);
      showAlert(t('mcp.updateFailed', { err: ((data && data.error) || t('mcp.unknownError')) }));
      return;
    }
    if (!data.updated && data.already_latest) {
      debugLog('[MCP] ' + serverId + ' already on latest v' + (data.version || '?'), 'info');
    } else {
      debugLog('[MCP] Updated ' + serverId + ' to v' + (data.version || '?') +
        (data.reconnected ? ' — reconnected with ' + (data.tools_count || 0) + ' tools' : ''), 'success');
    }
    _mcpClearPending(serverId, true);     // repopulate renders the fresh state
    await _populateMcpTab();
  } catch (e) {
    _mcpClearPending(serverId);
    showAlert(t('mcp.updateFailed', { err: _mcpErrDetail(e) }));
  }
}

// ── Manual add (code-free custom server) ──

/** Open the add-custom-server modal, resetting all fields. */
function _mcpOpenAddModal() {
  ['mcpNewName', 'mcpNewCommand', 'mcpNewArgs', 'mcpNewUrl', 'mcpNewEnv', 'mcpNewDesc'].forEach(function(id) {
    var el = document.getElementById(id);
    if (el) el.value = '';
  });
  var transport = document.getElementById('mcpNewTransport');
  if (transport) transport.value = 'stdio';
  _mcpTransportChanged();
  var status = document.getElementById('mcpAddStatus');
  if (status) { status.style.display = 'none'; status.textContent = ''; }
  var btn = document.getElementById('mcpAddSaveBtn');
  if (btn) { btn.disabled = false; btn.textContent = t('mcp.saveConnect'); }
  var overlay = document.getElementById('mcpAddOverlay');
  if (overlay) overlay.style.display = 'flex';
}

function _mcpCloseAddModal(evt) {
  if (evt && evt.target !== evt.currentTarget) return;
  var overlay = document.getElementById('mcpAddOverlay');
  if (overlay) overlay.style.display = 'none';
}

function _mcpTransportChanged() {
  var transport = (document.getElementById('mcpNewTransport') || {}).value || 'stdio';
  var stdioFields = document.getElementById('mcpStdioFields');
  var sseFields = document.getElementById('mcpSseFields');
  if (stdioFields) stdioFields.style.display = transport === 'stdio' ? '' : 'none';
  if (sseFields) sseFields.style.display = transport === 'sse' ? '' : 'none';
}

async function _mcpSaveServer() {
  var status = document.getElementById('mcpAddStatus');
  var saveBtn = document.getElementById('mcpAddSaveBtn');
  function _fail(msg) {
    if (status) {
      status.style.display = 'block';
      status.className = 'mcp-install-status error';
      status.textContent = '✕ ' + msg;
    } else {
      showAlert(msg);
    }
    if (saveBtn) { saveBtn.disabled = false; saveBtn.textContent = t('mcp.saveConnect'); }
  }

  var name = (document.getElementById('mcpNewName') || {}).value || '';
  name = name.trim();
  if (!name) { _fail(t('mcp.needName')); return; }

  var transport = (document.getElementById('mcpNewTransport') || {}).value || 'stdio';
  var payload = { name: name, transport: transport, enabled: true };

  if (transport === 'stdio') {
    payload.command = (document.getElementById('mcpNewCommand') || {}).value || '';
    var argsText = (document.getElementById('mcpNewArgs') || {}).value || '';
    payload.args = argsText.split('\n').map(function(s) { return s.trim(); }).filter(Boolean);
    if (!payload.command) { _fail(t('mcp.needCommand')); return; }
  } else {
    payload.url = (document.getElementById('mcpNewUrl') || {}).value || '';
    if (!payload.url) { _fail(t('mcp.needUrl')); return; }
  }

  // Parse env vars
  var envText = (document.getElementById('mcpNewEnv') || {}).value || '';
  if (envText.trim()) {
    payload.env = {};
    envText.split('\n').forEach(function(line) {
      var eq = line.indexOf('=');
      if (eq > 0) {
        payload.env[line.slice(0, eq).trim()] = line.slice(eq + 1).trim();
      }
    });
  }

  var desc = (document.getElementById('mcpNewDesc') || {}).value || '';
  if (desc.trim()) payload.description = desc.trim();

  if (saveBtn) { saveBtn.disabled = true; saveBtn.textContent = t('mcp.saving'); }
  if (status) {
    status.style.display = 'block';
    status.className = 'mcp-install-status info';
    status.textContent = t('mcp.startingName', { name: name });
  }

  try {
    var data = await Api.mcp.serverCreate(payload);
    if (!data || !data.ok) { _fail((data && data.error) || t('mcp.saveFailedNoConn')); return; }

    // Auto-connect
    await Api.mcp.connectOne(name);

    debugLog('[MCP] Server "' + name + '" saved & connected', 'success');
    if (status) {
      status.className = 'mcp-install-status success';
      status.textContent = t('mcp.savedConnected', { name: name });
    }
    setTimeout(function() {
      _mcpCloseAddModal();
      _populateMcpTab();
    }, 900);
  } catch (e) {
    _fail(_mcpErrDetail(e));
  }
}

function _applyDebugModeVisibility() {
  const visible = Boolean(runtimeScope._featureFlags?.debug_mode);
  for (const id of ['studioTopbarBtn', 'mobileStudio',
    'agentWorkflowSection', 'mobileWorkflowSection']) {
    const element = document.getElementById(id);
    if (element) element.style.display = visible ? '' : 'none';
  }
  /* A hidden experimental workflow must never keep owning future turns. The
   * already-accepted turn has an immutable config, so this paint/persist reset
   * only affects the next message when Debug Mode is switched off. */
  if (!visible && runtimeScope.activeFlow) {
    if (typeof _applyFlowUI === 'function') _applyFlowUI('');
    if (typeof captureActiveConversationSettings === 'function') {
      captureActiveConversationSettings();
    }
  }
}

function _applyTradingVisibility() {
  const element = document.getElementById('tradingModeBtn');
  if (element) element.style.display = runtimeScope._featureFlags?.trading_enabled ? '' : 'none';
}

function openTradingMode() { window.location.href = 'trading.html'; }

function _openActiveCompaction() {
  if (typeof runtimeScope.openCompactionViewer === 'function' && runtimeScope.activeConvId) {
    return runtimeScope.openCompactionViewer(runtimeScope.activeConvId);
  }
}

// BEGIN GENERATED LAZY RUNTIME PORTS — settings-presenters
Object.defineProperties(runtimeScope, {
  _serverConfig: {
    configurable: true,
    enumerable: false,
    get: () => _serverConfig,
    set: (value) => { _serverConfig = value; },
  },
  _modelPriceDisplayPolicy: {
    configurable: true,
    enumerable: false,
    get: () => _modelPriceDisplayPolicy,
    set: (value) => { if (value && typeof value === 'object') _modelPriceDisplayPolicy = value; },
  },
});
runtimeScope._addTemplateProvider = _addTemplateProvider;
runtimeScope._applyDebugModeVisibility = _applyDebugModeVisibility;
runtimeScope._applyTradingVisibility = _applyTradingVisibility;
runtimeScope._collectCostExperimentConfig = _collectCostExperimentConfig;
runtimeScope._collectFeishuConfig = _collectFeishuConfig;
runtimeScope._collectMtProviderConfig = _collectMtProviderConfig;
runtimeScope._collectProxyBypassDomains = _collectProxyBypassDomains;
runtimeScope._collectProxyPool = _collectProxyPool;
runtimeScope._collectResponsesExperimentControls = _collectResponsesExperimentControls;
runtimeScope._loadOAuthStatus = _loadOAuthStatus;
runtimeScope._populateAdvancedTab = _populateAdvancedTab;
runtimeScope._populateFeishuTab = _populateFeishuTab;
runtimeScope._populateMcpTab = _populateMcpTab;
runtimeScope._populateMtProviderSection = _populateMtProviderSection;
runtimeScope._populateNetworkTab = _populateNetworkTab;
runtimeScope._populateSearchTab = _populateSearchTab;
runtimeScope._renderMcpCatalog = _renderMcpCatalog;
runtimeScope._renderProvidersTab = _renderProvidersTab;
runtimeScope._stageModelRoutingProviderBundle = _stageModelRoutingProviderBundle;
// END GENERATED LAZY RUNTIME PORTS
// BEGIN GENERATED LAZY RUNTIME ACTIONS — settings-presenters
runtimeScope._addV2Alias = _addV2Alias;
runtimeScope._addV2HeaderRow = _addV2HeaderRow;
runtimeScope._applyMatrixRecommendations = _applyMatrixRecommendations;
runtimeScope._browserAccessDenyRead = _browserAccessDenyRead;
runtimeScope._catpawDisconnect = _catpawDisconnect;
runtimeScope._catpawImport = _catpawImport;
runtimeScope._clearConvCacheFromSettings = _clearConvCacheFromSettings;
runtimeScope._clearMatrixProbe = _clearMatrixProbe;
runtimeScope._closeProviderModelPicker = _closeProviderModelPicker;
runtimeScope._deleteModelRoutingProvider = _deleteModelRoutingProvider;
runtimeScope._deleteV2Credential = _deleteV2Credential;
runtimeScope._deleteV2HeaderRow = _deleteV2HeaderRow;
runtimeScope._filterProviderModelPicker = _filterProviderModelPicker;
runtimeScope._mcpApplyUpdate = _mcpApplyUpdate;
runtimeScope._mcpCloseAddModal = _mcpCloseAddModal;
runtimeScope._mcpCloseInstallModal = _mcpCloseInstallModal;
runtimeScope._mcpConnectAll = _mcpConnectAll;
runtimeScope._mcpDoInstall = _mcpDoInstall;
runtimeScope._mcpEnvPresetChanged = _mcpEnvPresetChanged;
runtimeScope._mcpFilterCatalog = _mcpFilterCatalog;
runtimeScope._mcpOpenAddModal = _mcpOpenAddModal;
runtimeScope._mcpOpenInstallModal = _mcpOpenInstallModal;
runtimeScope._mcpPurge = _mcpPurge;
runtimeScope._mcpQuickInstall = _mcpQuickInstall;
runtimeScope._mcpReconnect = _mcpReconnect;
runtimeScope._mcpSaveServer = _mcpSaveServer;
runtimeScope._mcpSetAllTools = _mcpSetAllTools;
runtimeScope._mcpSetCategory = _mcpSetCategory;
runtimeScope._mcpSetScope = _mcpSetScope;
runtimeScope._mcpToggleTool = _mcpToggleTool;
runtimeScope._mcpToggleToolsPanel = _mcpToggleToolsPanel;
runtimeScope._mcpTransportChanged = _mcpTransportChanged;
runtimeScope._mcpUninstall = _mcpUninstall;
runtimeScope._oauthConsumeReset = _oauthConsumeReset;
runtimeScope._oauthCopyAuthLink = _oauthCopyAuthLink;
runtimeScope._oauthCopyDeviceCode = _oauthCopyDeviceCode;
runtimeScope._oauthDeviceLogin = _oauthDeviceLogin;
runtimeScope._oauthLogin = _oauthLogin;
runtimeScope._oauthLogout = _oauthLogout;
runtimeScope._oauthManualSubmit = _oauthManualSubmit;
runtimeScope._onV2HeaderRowEdit = _onV2HeaderRowEdit;
runtimeScope._onV2KeyClearOverride = _onV2KeyClearOverride;
runtimeScope._onV2KeyToggle = _onV2KeyToggle;
runtimeScope._openActiveCompaction = _openActiveCompaction;
runtimeScope._openProviderModelPicker = _openProviderModelPicker;
runtimeScope._probeMatrixScope = _probeMatrixScope;
runtimeScope._proxyBypassDelete = _proxyBypassDelete;
runtimeScope._proxyBypassRefreshCount = _proxyBypassRefreshCount;
runtimeScope._proxyPoolDelete = _proxyPoolDelete;
runtimeScope._proxyPoolMove = _proxyPoolMove;
runtimeScope._proxyPoolSyncMeta = _proxyPoolSyncMeta;
runtimeScope._proxyPoolTest = _proxyPoolTest;
runtimeScope._proxyPoolToggleEditor = _proxyPoolToggleEditor;
runtimeScope._proxyPoolToggleUrlVisibility = _proxyPoolToggleUrlVisibility;
runtimeScope._proxyPoolUrlChanged = _proxyPoolUrlChanged;
runtimeScope._removeV2Alias = _removeV2Alias;
runtimeScope._removeV2Offering = _removeV2Offering;
runtimeScope._runMatrixProbe = _runMatrixProbe;
runtimeScope._searchProfileChanged = _searchProfileChanged;
runtimeScope._setMatrixAttempts = _setMatrixAttempts;
runtimeScope._setMatrixFilter = _setMatrixFilter;
runtimeScope._setMatrixQuery = _setMatrixQuery;
runtimeScope._setModelRoutingCollectionField = _setModelRoutingCollectionField;
runtimeScope._showTemplateMenu = _showTemplateMenu;
runtimeScope._stageV2CheckedCatalogModels = _stageV2CheckedCatalogModels;
runtimeScope._stageV2CustomModelFromPicker = _stageV2CustomModelFromPicker;
runtimeScope._startNewV2ApiKey = _startNewV2ApiKey;
runtimeScope._switchMtProvider = _switchMtProvider;
runtimeScope._syncResponsesExperimentUi = _syncResponsesExperimentUi;
runtimeScope._testMtProvider = _testMtProvider;
runtimeScope._testSearchBrowser = _testSearchBrowser;
runtimeScope._toggleMatrixAccess = _toggleMatrixAccess;
runtimeScope._toggleProviderModelPick = _toggleProviderModelPick;
runtimeScope._toggleV2KeyReveal = _toggleV2KeyReveal;
runtimeScope.addProvider = addProvider;
runtimeScope.openTradingMode = openTradingMode;
// END GENERATED LAZY RUNTIME ACTIONS
