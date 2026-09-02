/* ===== migrated source: main/main_toolbar_ui.js ===== */
/* ═══════════════════════════════════════════════════════════════════
   main toolbar ui — extracted from main.js (split 2026-05-28)

   Toolbar UI: model dropdown, presets, translation, browser and Agent Mode controls.

   This file is concatenated by Vite's module graph BEFORE main.js so
   the boot IIFE can reference these symbols. Symbols share `window`
   scope — no imports / exports needed.
   ═══════════════════════════════════════════════════════════════════ */

// ══════════════════════════════════════════════════════
//  Defensive fallback for isChatModel / applyCapabilityTaxonomy
//
//  core/model_caps.js is the SSOT for chat-vs-non-chat classification,
//  and normally loads BEFORE this file. But if the bundle ever ships
//  without it (stale bundler manifest, minifier regression, CDN partial
//  fetch, the migrated module graph drift), every model picker that calls the
//  bare identifier `isChatModel(m)` would throw ReferenceError and
//  strand the dropdown empty. To keep the dropdown alive we install a
//  local fallback here — a hardcoded copy of CHAT_EXCLUDED_CAPS from
//  lib/model_info/capability_taxonomy.py, byte-identical to the
//  literal in core/model_caps.js. When model_caps.js DID load, that
//  file's definitions run first and this block is a no-op.
//
//  Kept in lock-step with the Python SSOT by
//  tests/test_frontend_model_caps_bundled.py (parity + neuter).
// ══════════════════════════════════════════════════════
(function _installModelCapsFallback() {
  if (typeof window === 'undefined') return;
  var _FE_CHAT_EXCLUDED_FALLBACK = ['image_gen', 'embedding', 'transcription'];
  if (typeof runtimeScope.isChatModel !== 'function') {
    var _set = new Set(_FE_CHAT_EXCLUDED_FALLBACK);
    runtimeScope.isChatModel = function _isChatModelFallback(m) {
      if (!m) return true;
      var caps = m.capabilities;
      if (!caps || caps.length === 0) return true;
      for (var i = 0; i < caps.length; i++) if (_set.has(caps[i])) return false;
      return true;
    };
    // Reachable only when core/model_caps.js failed to load — the SSOT
    // version overrides this at its own IIFE and this branch never fires.
    try { console.warn('[Tofu] core/model_caps.js absent — using hardcoded chat-filter fallback in main_toolbar_ui.js'); } catch (_) {}
  }
  if (typeof runtimeScope.applyCapabilityTaxonomy !== 'function') {
    // Minimal shim so the /api/server-config ingestion path stays functional
    // even without model_caps.js — swap in the server's chat_excluded_caps
    // if provided. Same behavioural contract as the real one, minus the
    // dispatcher-set bookkeeping (frontend doesn't filter with that anyway).
    runtimeScope.applyCapabilityTaxonomy = function _applyCapabilityTaxonomyFallback(payload) {
      if (!payload || typeof payload !== 'object') return;
      var xs = payload.chat_excluded_caps;
      if (!Array.isArray(xs) || xs.length === 0) return;
      var _set2 = new Set(xs);
      runtimeScope.isChatModel = function _isChatModelFallback2(m) {
        if (!m) return true;
        var caps = m.capabilities;
        if (!caps || caps.length === 0) return true;
        for (var i = 0; i < caps.length; i++) if (_set2.has(caps[i])) return false;
        return true;
      };
    };
  }
})();

/** One-shot console warning when the model-caps SSOT is missing at call time.
 *  Kept debounced (per-page-load) so a big model list doesn't spam the console.
 *  Referenced by the guarded filters in this file and in
 *  frontend/src/runtime/settings/visibility_defaults.js. */
var _modelCapsMissingWarned = false;
function _warnModelCapsMissing() {
  if (_modelCapsMissingWarned) return;
  _modelCapsMissingWarned = true;
  try { console.warn('[Tofu] isChatModel unavailable — showing all models unfiltered (non-chat models may appear in the picker)'); } catch (_) {}
}
if (typeof window !== 'undefined') runtimeScope._warnModelCapsMissing = _warnModelCapsMissing;

// ── Toggles ──
function toggleThinking() {
  thinkingEnabled = !thinkingEnabled;
}

// ══════════════════════════════════════════════════════
// Two-tier capability dial (Chat / Studio)
//   SINGLE source of truth mirrored from the backend
//   (lib/tasks_pkg/chat_mode.chat_mode_defaults). The parity test
//   tests/test_chat_mode_parity.py asserts this table is byte-equal to the
//   Python one — keep them in lock-step.
//
//   Only the atomic flags a tier PINS are listed. Extras (browser/desktop/
//   imageGen/humanGuidance/autoTranslate) are orthogonal — a tier switch
//   never clobbers them.
//
//   (The old lean 'air' tier was merged into 'chat'; legacy air/pro persisted
//   in old convs normalise forward to 'chat' — see chat_mode.normalize.)
// ══════════════════════════════════════════════════════
const _CHAT_MODE_DEFAULTS = {
  chat: {
    searchMode: 'multi',
    fetchEnabled: true,
    codeExecEnabled: true,
    memoryEnabled: true,
  },
  studio: {
    searchMode: 'multi',
    fetchEnabled: true,
    memoryEnabled: true,
  },
};
if (typeof window !== 'undefined') runtimeScope._CHAT_MODE_DEFAULTS = _CHAT_MODE_DEFAULTS;

/* Paint the segmented control's active state + reflect the derived flags into
 * the atomic-flag setters. Does NOT persist or open modals — that's the
 * caller's job (setChatMode). Safe to call on restore. */
function _applyChatModeUI(mode) {
  // Normalise legacy tier codes (air/pro) forward to the merged 'chat' tier.
  mode = (mode === 'studio') ? 'studio' : 'chat';
  chatMode = mode;
  const d = _CHAT_MODE_DEFAULTS[mode] || {};
  if (typeof _applySearchModeUI === 'function') _applySearchModeUI(d.searchMode || 'multi');
  if (typeof _applyFetchEnabledUI === 'function') _applyFetchEnabledUI(d.fetchEnabled !== false);
  // codeExec: studio leaves it alone (run_command supersedes it in project
  // mode); chat pins it on explicitly.
  if (d.codeExecEnabled !== undefined && typeof _applyCodeExecUI === 'function') {
    _applyCodeExecUI(!!d.codeExecEnabled);
  }
  if (d.memoryEnabled !== undefined && typeof _applyMemoryUI === 'function') {
    _applyMemoryUI(!!d.memoryEnabled);
  }
  // ── Paint the popover trigger (icon + label) and the menu's selected row.
  //    The trigger mirrors the active tier's glyph so the collapsed control
  //    still communicates the current mode at a glance. ──
  const _MODE_LABEL = { chat: 'Chat', studio: 'Studio' };
  const _MODE_ICON = {
    chat: '<svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M21 15a2 2 0 0 1-2 2H7l-4 4V5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2z"/></svg>',
    studio: '<svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M22 19a2 2 0 0 1-2 2H4a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h5l2 3h9a2 2 0 0 1 2 2z"/></svg>',
  };
  const lbl = document.getElementById('chatModeLabel');
  if (lbl) lbl.textContent = _MODE_LABEL[mode] || 'Chat';
  const ic = document.getElementById('chatModeIcon');
  if (ic) ic.innerHTML = _MODE_ICON[mode] || _MODE_ICON.chat;
  const trig = document.getElementById('chatModeToggle');
  if (trig) trig.dataset.mode = mode;
  document.querySelectorAll('#chatModeMenu .chat-mode-item').forEach(el => {
    el.classList.toggle('selected', el.dataset.mode === mode);
  });
  if (typeof updateSubmenuCounts === 'function') updateSubmenuCounts();
}
if (typeof window !== 'undefined') runtimeScope._applyChatModeUI = _applyChatModeUI;

/* Open/close the mode popover (upward). Twin of toggleFlowMenu — one popover
 * open at a time; closes on outside click (handler below). */
function toggleChatModeMenu(e) {
  if (e) e.stopPropagation();
  const menu = document.getElementById('chatModeMenu');
  if (!menu) return;
  const willOpen = !menu.classList.contains('open');
  // Close the sibling flow menu so only one popover shows at once.
  _closeFlowMenu();
  menu.classList.toggle('open', willOpen);
}
if (typeof window !== 'undefined') runtimeScope.toggleChatModeMenu = toggleChatModeMenu;

function closeChatModeMenu() {
  const menu = document.getElementById('chatModeMenu');
  if (menu) menu.classList.remove('open');
}
if (typeof window !== 'undefined') runtimeScope.closeChatModeMenu = closeChatModeMenu;

// Close the mode menu on outside click (mirrors the flow menu handler).
if (typeof document !== 'undefined') {
  document.addEventListener('click', (e) => {
    if (!e.target.closest('#modeMenuWrapper')) {
      const menu = document.getElementById('chatModeMenu');
      if (menu) menu.classList.remove('open');
    }
  });
}

/* User clicked a tier. Studio is special: it REQUIRES a project, so clicking
 * it opens the project panel directly; the tier only becomes 'studio' once a
 * project is actually attached (mpApplyFolders → onProjectAttached). Clicking
 * Studio while a project is already attached just re-selects it. */
function setChatMode(mode) {
  if (mode === 'studio') {
    const hasProject = (typeof projectState !== 'undefined')
      && projectState && projectState.active && projectState.path;
    // The Studio segment IS the project affordance now (the standalone project
    // button is gone), so it must ALWAYS open the project panel — otherwise a
    // conv that is already in Studio has no way left to change its project
    // path (clicking Studio again would be a silent no-op).
    //
    // Open the panel FIRST and unconditionally: the panel opening must not
    // depend on the dial/state bookkeeping below succeeding. Previously the
    // has-project branch ran _applyChatModeUI + captureActiveConversationSettings BEFORE
    // opening — if either threw synchronously the panel never opened, so an
    // already-attached conv could never change its path (while attaching a
    // fresh one, which skips that bookkeeping, worked). The dial bookkeeping is
    // now best-effort and cannot block the affordance.
    if (typeof openProjectModal === 'function') openProjectModal();
    if (hasProject) {
      try {
        _applyChatModeUI('studio');
        captureActiveConversationSettings();
        debugLog('Mode: Studio (project attached)', 'success');
      } catch (err) {
        console.warn('[setChatMode] studio dial bookkeeping failed:', err);
      }
    }
    return;
  }
  // chat. Switching AWAY from studio while a project is attached would be
  // contradictory (studio ⟺ project); clearing the project is an explicit act
  // via the project panel, so here we only change the dial + flags. If a
  // project is attached and the user picks chat, we still detach-in-spirit by
  // clearing the project so the derived state stays truthful.
  if (mode !== 'studio'
      && typeof projectState !== 'undefined' && projectState
      && projectState.active && projectState.path
      && typeof clearProject === 'function') {
    clearProject();  // clears projectPath; async server reconcile is fire-and-forget
  }
  _applyChatModeUI(mode);
  captureActiveConversationSettings();
  debugLog('Mode: Chat', 'success');
}
if (typeof window !== 'undefined') runtimeScope.setChatMode = setChatMode;

/* Called by mpApplyFolders after a project is successfully attached — promote
 * the dial to Studio (the tier IS "a project is attached"). Kept separate from
 * setChatMode so the project path owns the promotion. The promotion is
 * persisted immediately — without it conv.chatMode keeps the stale tier until
 * the next unrelated toggle, and a reload in between restores the wrong dial. */
function onProjectAttached() {
  if (chatMode !== 'studio') {
    _applyChatModeUI('studio');
    if (typeof captureActiveConversationSettings === 'function') captureActiveConversationSettings();
  }
}
if (typeof window !== 'undefined') runtimeScope.onProjectAttached = onProjectAttached;

/* Called by clearProject — a project-less chat is never Studio; fall back to
 * the everyday Chat tier AND persist the fallback. Without the persist,
 * conv.chatMode kept 'studio' with an empty projectPath, and the restore path
 * trusting that stored tier resurrected Studio on the next reload/conv-switch
 * even though no project was attached. */
function onProjectCleared() {
  if (chatMode === 'studio') {
    _applyChatModeUI('chat');
    if (typeof captureActiveConversationSettings === 'function') captureActiveConversationSettings();
  }
}
if (typeof window !== 'undefined') runtimeScope.onProjectCleared = onProjectCleared;

// ══════════════════════════════════════════════════════
// Agent interaction mode — Standard / Plan / Autopilot
//
// One radio surface projects the established planMode / autopilotEnabled
// wire flags. Chat/Studio remains an orthogonal capability dial.
// ══════════════════════════════════════════════════════

const _AGENT_MODE_LABEL_KEYS = Object.freeze({
  standard: 'toolbar.standardMode',
  plan: 'toolbar.planMode',
  autopilot: 'toolbar.autopilot',
});

function _paintAgentModeUI() {
  const mode = resolveAgentMode(planMode, autopilotEnabled);
  const workflowSelected = !!activeFlow;
  const trigger = document.getElementById('agentModeToggle');
  if (trigger) {
    trigger.dataset.mode = workflowSelected ? 'workflow' : mode;
    trigger.classList.toggle(
      'has-active', workflowSelected || mode !== 'standard');
  }
  const label = document.getElementById('agentModeLabel');
  const labelKey = _AGENT_MODE_LABEL_KEYS[mode];
  if (label) {
    if (workflowSelected) {
      label.removeAttribute('data-i18n');
      label.textContent = _flowDisplayName(activeFlow);
    } else {
      label.dataset.i18n = labelKey;
      label.textContent = typeof t === 'function' ? t(labelKey) : labelKey;
    }
  }
  document.querySelectorAll('#agentModeMenu .agent-mode-item').forEach((item) => {
    const selected = !workflowSelected && item.dataset.mode === mode;
    item.classList.toggle('selected', selected);
    item.classList.toggle('active', selected);
    item.setAttribute('aria-checked', selected ? 'true' : 'false');
  });
}

/* Paint-only and atomic: restore paths use this without persisting. */
function _applyAgentModeUI(mode) {
  const flags = agentModeFlags(mode);
  planMode = flags.planMode;
  autopilotEnabled = flags.autopilotEnabled;
  const autopilotBadge = document.getElementById('autopilotBadge');
  if (autopilotBadge) autopilotBadge.style.display = autopilotEnabled ? '' : 'none';
  _paintAgentModeUI();
}
if (typeof window !== 'undefined') runtimeScope._applyAgentModeUI = _applyAgentModeUI;

/* Compatibility paint ports for retained callers. User actions go through
 * setAgentMode(), which also owns persistence and Autopilot disarm. */
function _applyPlanModeUI(on) {
  _applyAgentModeUI(on ? 'plan'
    : resolveAgentMode(false, autopilotEnabled));
}
if (typeof window !== 'undefined') runtimeScope._applyPlanModeUI = _applyPlanModeUI;

/* One accepted turn owns an immutable interaction config. All controls that
 * can change the loop owner (Agent mode, Flow, or a Plan-required capability)
 * consult the same guard so a secondary switch cannot visually bypass the
 * disabled Agent-mode trigger during start/stop handshakes. */
function _agentInteractionChangeBlocked() {
  const conv = typeof getActiveConv === 'function' ? getActiveConv() : null;
  if (!conv) return false;
  return Boolean(
    (typeof convIsBusy === 'function' && convIsBusy(conv))
    || conv._translating || conv._genStartCtrl || conv._finishingStream
  );
}
if (typeof window !== 'undefined') {
  runtimeScope._agentInteractionChangeBlocked = _agentInteractionChangeBlocked;
}

function _setAgentModeLocked(on) {
  const locked = !!on;
  const trigger = document.getElementById('agentModeToggle');
  [trigger, document.getElementById('flowToggle')].forEach((control) => {
    if (!control) return;
    control.disabled = locked;
    control.setAttribute('aria-disabled', locked ? 'true' : 'false');
  });
  /* A disabled trigger swallows the click outright — swap the tooltip so the
   * locked state explains itself instead of reading as a dead button. Ride
   * data-i18n-title so a language repaint keeps the locked copy while the
   * lock holds and restores the base copy after it lifts. */
  if (trigger) {
    const titleKey = locked
      ? 'toolbar.agentModeLockedTooltip' : 'toolbar.agentModeTooltip';
    trigger.setAttribute('data-i18n-title', titleKey);
    if (typeof t === 'function') trigger.title = t(titleKey);
  }
  document.querySelectorAll(
    '#agentModeMenu button, .mobile-agent-mode-item, #mobileFlow, '
      + '#flowMenuList button',
  )
    .forEach((item) => { item.disabled = locked; });
  if (locked) {
    const submenu = document.getElementById('submenuAgentMode');
    submenu?.classList.remove('open');
    submenu?.querySelector('.submenu-trigger')?.classList.remove('open');
    trigger?.setAttribute('aria-expanded', 'false');
    _flowMenuRenderGeneration++;
  }
}
if (typeof window !== 'undefined') runtimeScope._setAgentModeLocked = _setAgentModeLocked;

function setAgentMode(requestedMode) {
  if (_agentInteractionChangeBlocked()) return false;
  const mode = normalizeAgentMode(requestedMode);
  const wasAutopilot = autopilotEnabled;
  if (typeof _applyFlowUI === 'function') _applyFlowUI('');
  _applyAgentModeUI(mode);
  if (typeof _applyImageGenUI === 'function') _applyImageGenUI(false);
  if (mode === 'plan' && typeof _applyHumanGuidanceUI === 'function') {
    _applyHumanGuidanceUI(true);
  }
  if (wasAutopilot && mode !== 'autopilot') _disarmAutopilot();
  captureActiveConversationSettings();
  if (typeof updateSubmenuCounts === 'function') updateSubmenuCounts();
  _closeAgentModeMenu();
  debugLog(`Agent mode: ${mode}`, 'success');
  return true;
}
if (typeof window !== 'undefined') runtimeScope.setAgentMode = setAgentMode;

/* Legacy callable names remain for extensions and old cached markup. */
function togglePlanMode() {
  return setAgentMode(planMode ? 'standard' : 'plan');
}
if (typeof window !== 'undefined') runtimeScope.togglePlanMode = togglePlanMode;

/* Derive the correct tier from the current atomic flags — used on restore of
 * an OLD conversation that has no stored chatMode (pre-feature convs). With
 * the air/pro merge there are only two tiers: a project ⇒ studio, else chat. */
function _deriveChatModeFromFlags(conv) {
  if (conv && conv.projectPath) return 'studio';
  return 'chat';
}
if (typeof window !== 'undefined') runtimeScope._deriveChatModeFromFlags = _deriveChatModeFromFlags;
/* Populate model dropdown dynamically from the registered models list.
 * Called once at startup from _loadServerConfigAndPopulate(). */
function _populateModelDropdown(models) {
  /* Write into the inner list container, NOT #presetDropdown itself — the
   * dropdown now also holds the folded-in thinking-depth footer, which must
   * survive a model-list rebuild. Fall back to the dropdown for older markup. */
  const dropdown = document.getElementById("presetDropdownList")
    || document.getElementById("presetDropdown");
  if (!dropdown || !models || models.length === 0) return;
  _registeredModels = models;
  dropdown.innerHTML = '';

  /* Filter out hidden models and non-chat models (but keep current model visible).
   * isChatModel comes from core/model_caps.js — single source of truth for
   * "is this model a chat model?", read from the server taxonomy at boot. */
  const visibleModels = models.filter(m => {
    if (m.model_id === config.model) return true;  // always keep current model
    if (_hiddenModels.has(m.model_id)) return false;
    // Guard: if core/model_caps.js failed to load (stale bundle, minifier
    // regression, CDN partial fetch, …), fall through to "show everything"
    // rather than throw ReferenceError and leave the dropdown empty. An ASR
    // model leaking into the picker is a known small annoyance; a black
    // dropdown is a hard failure. See tests/test_frontend_model_caps_bundled.py.
    if (typeof runtimeScope.isChatModel !== 'function') { _warnModelCapsMissing(); return true; }
    return runtimeScope.isChatModel(m);
  });

  /* Group models by the SHARED brand rule (core/model_group.js) — NOT by
   * provider_id. Grouping by provider leaks the backend's wire detail: the
   * Meituan gateway serves openai on one face and anthropic on another
   * (sankuai vs sankuai_anthropic), which the picker would render as TWO
   * "Meituan" sections. The settings preset tab groups by the same brand
   * rule, so the two lists can never disagree. Degrade to a per-provider
   * grouping only if the shared module failed to load (stale bundle). */
  const _hasGroup = (typeof runtimeScope.modelGroupKey === 'function'
                     && typeof runtimeScope.modelGroupLabel === 'function');
  const grouped = {};  // groupKey → { name, models: [] }
  for (const m of visibleModels) {
    const _entryProvider = { brand: m.brand, name: m.provider_name };
    const gkey = _hasGroup
      ? runtimeScope.modelGroupKey(_entryProvider, m)
      : (m.provider_id || 'default');
    const gname = _hasGroup
      ? runtimeScope.modelGroupLabel(gkey, m.provider_name)
      : (m.provider_name || gkey);
    if (!grouped[gkey]) grouped[gkey] = { name: gname, models: [] };
    grouped[gkey].models.push(m);
  }

  /* Order the list the way the user READS it.
   *
   * Two axes, both previously unordered:
   *   - Section order was Object.keys() insertion order, i.e. provider order in
   *     server_config.json — arbitrary relative to anything on screen.
   *   - Within a section, models arrived in model_id order (the Settings cold
   *     sort writes that back), but the ROW shows _modelShortName(id). Those
   *     differ: `yuju-claude-opus-5-evaDaily` renders as "Claude Opus 5" yet
   *     sorted under 'y'.
   *
   * The comparator is the shared one from settings/branding.js, so this picker
   * and the Settings model list can never disagree. Guarded: a stale bundle
   * missing branding.js leaves the list unsorted rather than throwing and
   * stranding an empty dropdown (same rationale as the isChatModel guard). */
  const _canSort = (typeof _compareModelsByDisplayName === 'function');
  const groupKeys = Object.keys(grouped);
  if (_canSort) {
    groupKeys.sort((x, y) => {
      const nx = String((grouped[x] && grouped[x].name) || x);
      const ny = String((grouped[y] && grouped[y].name) || y);
      return _compareModelsByDisplayName(nx, ny);
    });
  }
  /* ── Row builder (shared by faces, fold members and the recents strip) ──
   * Icon rule = the SHARED group rule (core/model_group.js). A raw m.brand
   * of 'oauth' / 'adapter' is a CREDENTIAL KIND, not a vendor — rendered
   * literally it has no _BRAND_ICONS entry and lands on the grey generic
   * box, even though the row sits under the real vendor section the group
   * rule found (e.g. ChatGPT-subscription GPTs under "OPENAI"). Resolve
   * credential kinds through the same modelGroupKey inputs the grouping
   * pass used, so a row's icon can never disagree with its own section
   * header. Empty brand keeps the legacy per-model detect (an aggregator
   * group still shows each model's own vendor icon). */
  const _row = (m, opts) => {
    opts = opts || {};
    const _rowBrand = (m.brand || '').trim();
    const _rowCredKind = (_rowBrand === 'oauth' || _rowBrand === 'adapter');
    const brand = (_rowBrand && !_rowCredKind)
      ? _rowBrand
      : (_rowCredKind && _hasGroup)
        ? runtimeScope.modelGroupKey({ brand: m.brand, name: m.provider_name }, m)
        : (typeof _detectBrand === 'function' ? _detectBrand(m.model_id) : 'generic');
    const item = document.createElement('div');
    item.className = 'preset-dropdown-item' + (opts.sub ? ' ps-dd-sub-item' : '');
    item.setAttribute('data-value', m.model_id);
    if (opts.section) item.setAttribute('data-section', opts.section);
    item.onclick = function() { selectModel(m.model_id); };
    const isActive = m.model_id === (config.model || serverModel);
    if (isActive) item.classList.add('active');
    const iconSpan = document.createElement('span');
    iconSpan.className = 'ps-dd-icon';
    if (typeof _brandSvg === 'function') {
      iconSpan.innerHTML = _brandSvg(brand, 14);
    } else {
      iconSpan.textContent = '✦';
    }
    const nameSpan = document.createElement('span');
    nameSpan.className = 'ps-dd-label';
    /* Folded mirror rows all share one friendly name — the raw id is the
     * only thing that tells two interchangeable spellings apart. */
    const label = opts.rawId ? m.model_id
      : (typeof _modelShortName === 'function' ? _modelShortName(m.model_id) : m.model_id);
    nameSpan.textContent = label;
    nameSpan.title = m.model_id;
    item.setAttribute('data-search',
      (label + ' ' + m.model_id + ' ' + (opts.extraSearch || '')).toLowerCase());
    item.appendChild(iconSpan);
    item.appendChild(nameSpan);
    return item;
  };

  /* Render ONE display unit (core/model_fold.js contract). Folded units get
   * a badge (alias mirrors) or an expander row (older versions) whose
   * sub-container starts open when the CURRENT model lives inside — the
   * fold never hides what the user is running. */
  const _renderUnit = (unit, gkey) => {
    const face = unit.face;
    const memberHaystack = unit.members.map(m =>
      m.model_id + ' ' + (typeof _modelShortName === 'function'
        ? _modelShortName(m.model_id) : m.model_id)).join(' ');
    const faceRow = _row(face, { section: gkey, extraSearch: memberHaystack });
    const activeMember = unit.members.find(
      m => m.model_id === (config.model || serverModel));
    const isFolded = unit.kind !== 'single' && unit.members.length > 1;
    if (!isFolded) {
      dropdown.appendChild(faceRow);
      return;
    }
    const sub = document.createElement('div');
    sub.className = 'ps-dd-sub';
    sub.setAttribute('data-section', gkey);
    if (unit.kind === 'alias') {
      const badge = document.createElement('span');
      badge.className = 'ps-dd-fold-badge';
      badge.textContent = '×' + unit.members.length;
      badge.title = t('msg.mirrorFoldTip', { count: unit.members.length });
      badge.onclick = function(e) {
        e.stopPropagation();
        sub.classList.toggle('open');
        badge.classList.toggle('open');
      };
      faceRow.appendChild(badge);
      for (const m of unit.members) {
        if (m.model_id === face.model_id) continue;
        sub.appendChild(_row(m, { sub: true, rawId: true, section: gkey }));
      }
    } else { /* family */
      for (const child of (unit.children || [])) {
        if (child.face.model_id === face.model_id && child.kind === 'single') continue;
        if (child.face.model_id === face.model_id) {
          /* primary is itself an alias unit — keep its mirrors reachable */
          for (const m of child.members) {
            if (m.model_id === face.model_id) continue;
            sub.appendChild(_row(m, { sub: true, rawId: true, section: gkey }));
          }
          continue;
        }
        sub.appendChild(_row(child.face, { sub: true, section: gkey }));
      }
      const expander = document.createElement('div');
      expander.className = 'ps-dd-expander';
      expander.setAttribute('data-section', gkey);
      expander.innerHTML = '<span>' + t('msg.moreVersions', { count: unit.members.length - 1 })
        + '</span><svg class="ps-dd-expander-arrow" width="10" height="10" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><polyline points="6 9 12 15 18 9"/></svg>';
      expander.onclick = function(e) {
        e.stopPropagation();
        sub.classList.toggle('open');
        expander.classList.toggle('open');
      };
      dropdown.appendChild(faceRow);
      dropdown.appendChild(expander);
      dropdown.appendChild(sub);
      if (activeMember && activeMember.model_id !== face.model_id) {
        sub.classList.add('open');
        expander.classList.add('open');
      }
      return;
    }
    dropdown.appendChild(faceRow);
    dropdown.appendChild(sub);
    if (activeMember && activeMember.model_id !== face.model_id) {
      sub.classList.add('open');
      const badge = faceRow.querySelector('.ps-dd-fold-badge');
      if (badge) badge.classList.add('open');
    }
  };

  /* Fold each brand section into display units. Guard: a stale bundle
   * missing core/model_fold.js degrades to the old flat list (same
   * rationale as the isChatModel guard above). */
  const _hasFold = (typeof runtimeScope.modelDisplayUnits === 'function');

  /* ── Recent models strip (localStorage, core/model_fold.js) — only worth
   * the vertical space when the list is actually long. Never shows the
   * current model (it is already on the toggle). */
  const _recents = (_hasFold && visibleModels.length > 8
    && typeof runtimeScope.recentModels === 'function')
    ? runtimeScope.recentModels().filter(id =>
        id !== (config.model || serverModel)
        && visibleModels.some(m => m.model_id === id))
    : [];
  if (_recents.length > 0) {
    const rlabel = document.createElement('div');
    rlabel.className = 'ps-dd-section-label';
    rlabel.setAttribute('data-section', '__recent');
    rlabel.textContent = t('toolbar.recentModels');
    dropdown.appendChild(rlabel);
    for (const rid of _recents) {
      const m = visibleModels.find(x => x.model_id === rid);
      dropdown.appendChild(_row(m, { section: '__recent' }));
    }
  }

  for (const gkey of groupKeys) {
    const group = grouped[gkey];
    if (_canSort) group.models.sort(_compareModelsByDisplayName);
    /* Only show section headers when there are multiple groups */
    if (groupKeys.length > 1) {
      const labelDiv = document.createElement('div');
      labelDiv.className = 'ps-dd-section-label';
      labelDiv.textContent = group.name;
      labelDiv.setAttribute('data-section', gkey);
      dropdown.appendChild(labelDiv);
    }

    const units = _hasFold
      ? runtimeScope.modelDisplayUnits(group.models)
      : group.models.map(m => ({ kind: 'single', face: m, members: [m] }));
    for (const unit of units) _renderUnit(unit, gkey);
  }

  /* ── Search box (lives OUTSIDE the rebuilt list in #presetDropdown) —
   * shown once the list is long enough that scrolling costs more than
   * typing. Filtering is pure display toggling; folds auto-open on hits. */
  const searchWrap = document.getElementById('presetDropdownSearch');
  if (searchWrap) {
    const searchInput = document.getElementById('presetDropdownSearchInput');
    searchWrap.style.display = visibleModels.length > 10 ? '' : 'none';
    if (searchInput && !searchInput.dataset.wired) {
      searchInput.dataset.wired = '1';
      searchInput.addEventListener('input', () => _filterModelDropdown(searchInput.value));
      searchInput.addEventListener('keydown', (e) => {
        /* Keep global hotkeys out of the search field; Enter picks the
         * first visible row; Esc clears, then closes on a second press. */
        e.stopPropagation();
        if (e.key === 'Enter') {
          const first = dropdown.querySelector('.preset-dropdown-item:not([style*="none"])');
          if (first) first.click();
        } else if (e.key === 'Escape') {
          if (searchInput.value) {
            searchInput.value = '';
            _filterModelDropdown('');
          } else {
            document.getElementById('presetWrapper')?.classList.remove('open');
          }
        }
      });
      searchInput.addEventListener('click', (e) => e.stopPropagation());
    }
    if (searchInput) {
      searchInput.value = '';
      _filterModelDropdown('');
    }
  }

  /* Show a hint when there are many models, suggesting to hide unused ones in Settings */
  if (visibleModels.length > 10) {
    const hint = document.createElement('div');
    hint.className = 'ps-dd-hint';
    hint.innerHTML = '<svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="12" cy="12" r="10"/><path d="M12 16v-4M12 8h.01"/></svg>'
      + '<span>' + t('msg.tooManyModels') + '</span>';
    hint.onclick = function(e) {
      e.stopPropagation();
      document.getElementById("presetWrapper")?.classList.remove("open");
      if (typeof openSettings === 'function') openSettings();
      if (typeof switchSettingsTab === 'function') switchSettingsTab('preset');
    };
    dropdown.appendChild(hint);
  }
}

/* Search-filter the model dropdown — pure display toggling over the rows
 * _populateModelDropdown rendered (data-search haystack per row,
 * data-section per row/label/sub-container). A query auto-OPENS folded
 * units whose members hit (search is the moment the user wants the long
 * tail, so hiding it behind a click would defeat the point); clearing the
 * query restores the folded/expanded state the rows were rendered with. */
function _filterModelDropdown(query) {
  const dropdown = document.getElementById("presetDropdownList")
    || document.getElementById("presetDropdown");
  if (!dropdown) return;
  const q = String(query || '').trim().toLowerCase();
  const rows = dropdown.querySelectorAll('.preset-dropdown-item');
  if (!q) {
    rows.forEach(r => { r.style.display = ''; });
    dropdown.querySelectorAll('.ps-dd-sub').forEach(s => {
      s.classList.remove('search-open');
    });
    dropdown.querySelectorAll('.ps-dd-expander').forEach(x => {
      x.style.display = '';
    });
    dropdown.querySelectorAll('.ps-dd-section-label').forEach(l => {
      l.style.display = '';
    });
    return;
  }
  rows.forEach(r => {
    const hay = (r.getAttribute('data-search') || '');
    r.style.display = hay.indexOf(q) >= 0 ? '' : 'none';
  });
  /* Force open sub-containers holding a visible member row; hide expanders
   * whose section-subtree has nothing to expand to. */
  dropdown.querySelectorAll('.ps-dd-sub').forEach(s => {
    const anyVisible = Array.from(s.querySelectorAll('.preset-dropdown-item'))
      .some(r => r.style.display !== 'none');
    s.classList.toggle('search-open', anyVisible);
  });
  dropdown.querySelectorAll('.ps-dd-expander').forEach(x => {
    x.style.display = 'none';  // the fold is already open while searching
  });
  /* Section labels vanish when their whole section filtered out. */
  dropdown.querySelectorAll('.ps-dd-section-label').forEach(l => {
    const sec = l.getAttribute('data-section');
    const anyVisible = Array.from(
      dropdown.querySelectorAll('.preset-dropdown-item[data-section="' + sec + '"]'))
      .some(r => r.style.display !== 'none');
    l.style.display = anyVisible ? '' : 'none';
  });
}

/* Load the model list from server config and populate the dropdown.
 * Falls back to default models if config doesn't include a models list. */
function _loadServerConfigAndPopulate() {
  Api.serverConfig.get()
    .then(data => {
      if (!data) return;
      let models = data.dropdown_models;
      if (!models || models.length === 0) {
        /* Fallback: use the server model if available */
        models = serverModel ? [{ model_id: serverModel }] : [];
      }
      /* Build pricing cache from models data if available.
       * Used ONLY by settings.js to render the model-picker pricing
       * column. Cost-from-usage math is server-authoritative now
       * (lib/cost.py + POST /api/v1/messages/cost). */
      if (data.model_pricing) {
        _modelPricingCache = data.model_pricing;
      }
      /* (Per-provider pricing overrides used to be cached here for the
       * old client-side calcCostCny.  That's been migrated to
       * lib/pricing.py — provider overrides are resolved server-side
       * via lookup_pricing(model, provider_id).  Settings.js doesn't
       * need provider scoping for its display.) */
      /* Capture upload-shrink policy so compressImage() mirrors the backend.
       * See routes/upload.py:get_upload_policy(). Single source of truth — no
       * more frontend-vs-backend threshold drift (was 1024/q=0.85 vs 2048/q=0.90). */
      if (data.upload && typeof data.upload === 'object') {
        runtimeScope._uploadShrinkPolicy = data.upload;
      }
      /* Capture context-window policy so the Context Health Bar mirrors the
       * backend exactly. See lib/tasks_pkg/compaction.build_context_policy().
       * Single source of truth — no more frontend-vs-backend limit/threshold
       * drift (the JS table was stuck at 0.82 vs the real 0.90, and a stale
       * per-model regex table). */
      if (data.context && typeof data.context === 'object') {
        runtimeScope._contextPolicy = data.context;
        if (typeof runtimeScope.updateContextBar === 'function') runtimeScope.updateContextBar();
      }
      /* Capture translation policy (stale-partial heuristic threshold) so
       * translation.js mirrors the backend. See lib/text_lang.py. */
      if (data.translation && typeof data.translation === 'object') {
        runtimeScope._translationPolicy = data.translation;
      }
      /* Ingest capability taxonomy (SSOT for chat / non-chat capability
       * classification). Applied BEFORE the model-list filters below so
       * isChatModel(m) uses the server's shape, not the hardcoded fallback.
       * See lib/model_info/capability_taxonomy.py + core/model_caps.js. */
      if (data.capability_taxonomy && typeof applyCapabilityTaxonomy === 'function') {
        applyCapabilityTaxonomy(data.capability_taxonomy);
      }
      /* Load hidden models from server config */
      _hiddenModels = new Set(data.hidden_models || []);
      _hiddenIgModels = new Set(data.hidden_ig_models || []);
      /* Load IG models now that _hiddenIgModels is populated (avoids race condition
       * where the old setTimeout(2000) could fire before this config fetch completes,
       * causing hidden models to still appear in the IG picker). */
      if (typeof _loadIgModels === 'function') {
        _igModelsLoaded = true;
        _loadIgModels();
      }
      /* Sync serverModel with the configured default model from Settings.
       * Without this, _resetToolsToDefaults() (called on new chat) would always
       * use the hardcoded initial serverModel instead of the user's configured
       * default model from the Settings "默认模型" dropdown. */
      const cfgDefault = data.model_defaults && data.model_defaults.default_model;
      if (cfgDefault) {
        serverModel = cfgDefault;
      }
      _populateModelDropdown(models);

      /* Validate that config.model actually exists among the available models.
       * On fresh deploys (e.g. open-source), config.model may be a hardcoded default
       * (like "aws.claude-opus-4.6") that doesn't exist in the user's provider.
       * If so, fall back to serverModel (from server config) or the first available
       * chat model — pick randomly to avoid always landing on the same one. */
      const chatModels = (models || []).filter(m => {
        if (_hiddenModels.has(m.model_id)) return false;
        if (typeof runtimeScope.isChatModel !== 'function') { _warnModelCapsMissing(); return true; }
        return runtimeScope.isChatModel(m);
      });
      const availableIds = new Set(chatModels.map(m => m.model_id));
      const currentModel = config.model || serverModel;
      if (currentModel && !availableIds.has(currentModel)) {
        /* Current model not available — pick a valid one */
        let fallback = '';
        if (serverModel && availableIds.has(serverModel)) {
          fallback = serverModel;
        } else if (chatModels.length > 0) {
          /* Pick a random model so different users don't all land on the same one */
          fallback = chatModels[Math.floor(Math.random() * chatModels.length)].model_id;
        }
        if (fallback) {
          console.warn('[Config] Model "%s" not available in providers, falling back to "%s"', currentModel, fallback);
          config.model = fallback;
          try { localStorage.setItem("claude_client_config", JSON.stringify(config)); }
          catch (_e) { /* best-effort */ }
        }
      }

      /* Re-apply model UI now that dropdown is populated.
       * Pass null when the current value is only a provisional default, so the
       * repaint PRESERVES its provenance instead of promoting a fallback into
       * a "user choice" that the write-back sites would then persist. */
      _applyModelUI(config._modelIsProvisional ? null : config.model);

      /* Auto-open settings if ?setup=1 (from bootstrap) or no API keys configured */
      _maybeAutoOpenSettings(data);
    })
    .catch(e => {
      console.warn('[_loadServerConfigAndPopulate] Failed:', e);
      /* Fallback with server model only */
      _populateModelDropdown(
        serverModel ? [{ model_id: serverModel }] : []
      );
      /* Same provenance-preserving repaint as the success path above. */
      _applyModelUI(config._modelIsProvisional ? null : config.model);
    });
}

/* Auto-open settings to the API tab if the user just came from bootstrap
 * (?setup=1) or if no API keys are configured at all. Runs once on boot. */
function _maybeAutoOpenSettings(serverConfigData) {
  const params = new URLSearchParams(window.location.search);
  const fromBootstrap = params.get('setup') === '1';
  // Count total API keys across all providers
  const providers = serverConfigData.providers || [];
  const totalKeys = providers.reduce((sum, p) => sum + (p.api_keys || []).length, 0);
  const noKeys = totalKeys === 0;

  if (fromBootstrap || noKeys) {
    // Clean up the URL so ?setup=1 doesn't persist on reload
    if (fromBootstrap) {
      const cleanUrl = window.location.pathname + window.location.hash;
      window.history.replaceState(null, '', cleanUrl);
    }
    // Open settings after a short delay for the UI to settle
    setTimeout(() => {
      /* The first-run wizard supersedes the bare "open Settings to the
       * providers tab" behaviour: it asks the ONE question a new user can
       * answer (API key vs subscription) and drives the existing surfaces
       * from there. ?setup=1 forces it past the dismissal flag; the no-keys
       * trigger respects it so a skipped wizard never re-nags. */
      if (typeof maybeShowOnboarding === 'function' &&
          maybeShowOnboarding({ force: fromBootstrap })) {
        return;
      }
      // Wizard module absent (stale bundle) — the old surface still works.
      if (typeof openSettings === 'function') {
        openSettings();
        // Switch to the API/providers tab
        if (typeof switchSettingsTab === 'function') {
          switchSettingsTab('api');
        }
        // Show a helpful hint
        const hint = document.getElementById('settingsStatusHint');
        if (hint) {
          hint.textContent = noKeys
            ? '⚠️ No API keys configured — please add a provider to get started.'
            : '✅ Server started successfully! Review your API configuration below.';
          hint.style.color = noKeys ? '#f7768e' : '#9ece6a';
        }
      }
    }, 500);
  }
}

function togglePresetDropdown(e) {
  e.stopPropagation();
  const wrapper = document.getElementById("presetWrapper");
  wrapper.classList.toggle("open");
  // Close dropdown when clicking anywhere else
  if (wrapper.classList.contains("open")) {
    const closeHandler = function (ev) {
      if (!wrapper.contains(ev.target)) {
        wrapper.classList.remove("open");
        document.removeEventListener("click", closeHandler);
      }
    };
    // Delay so the current click event doesn't immediately trigger close
    setTimeout(() => document.addEventListener("click", closeHandler), 0);
  }
}
function selectModel(modelId) {
  _applyModelUI(modelId);
  /* Record the pick for the picker's recents strip (core/model_fold.js).
   * Guarded — a stale bundle missing the section just skips the strip. */
  if (typeof runtimeScope.pushRecentModel === 'function') {
    runtimeScope.pushRecentModel(modelId);
  }
  try { localStorage.setItem("claude_client_config", JSON.stringify(config)); }
  catch (e) { debugLog(`[selectModel] localStorage save failed: ${e.message}`, 'error'); }
  captureActiveConversationSettings();
  const depthSuffix = _isThinkingCapable(config.model) && config.thinkingDepth
    ? ` [${config.thinkingDepth.toUpperCase()}]`
    : '';
  debugLog(`Model: ${config.model}${depthSuffix}`, "success");
}
function toggleAutoTranslate() {
  autoTranslate = !autoTranslate;
  localStorage.setItem("claude_auto_translate", JSON.stringify(autoTranslate));
  const btn = document.getElementById("translateToggle");
  const badge = document.getElementById("translateBadge");
  if (btn) btn.classList.toggle("active", autoTranslate);
  if (badge) badge.style.display = autoTranslate ? "" : "none";
  captureActiveConversationSettings();
  debugLog(`Auto-Translate: ${autoTranslate ? "ON" : "OFF"}`, "success");

  // One-time hint about <notranslate> when first enabling
  if (autoTranslate && !localStorage.getItem("claude_translate_hint_shown")) {
    localStorage.setItem("claude_translate_hint_shown", "1");
    showToast(
      "", "Translation Tip",
      "Select text and press Ctrl+J to wrap it in &lt;notranslate&gt; — that part won't be translated.",
      8000
    );
  }
}
function _applyAutoTranslateUI(enabled) {
  if (typeof enabled !== "undefined") {
    autoTranslate = !!enabled;
    localStorage.setItem(
      "claude_auto_translate",
      JSON.stringify(autoTranslate),
    );
  }
  const btn = document.getElementById("translateToggle");
  const badge = document.getElementById("translateBadge");
  if (btn) btn.classList.toggle("active", autoTranslate);
  if (badge) badge.style.display = autoTranslate ? "" : "none";
}

// ══════════════════════════════════════════════════════
// Toolbar Sub-menus — dropdown grouping for tool toggles
// ══════════════════════════════════════════════════════
function toggleSubmenu(id) {
  const el = document.getElementById(id);
  if (!el) return;
  const wasOpen = el.classList.contains("open");
  // close all sub-menus first
  document.querySelectorAll(".toolbar-submenu.open").forEach(s => {
    s.classList.remove("open");
    const t = s.querySelector(".submenu-trigger");
    if (t) {
      t.classList.remove("open");
      t.setAttribute('aria-expanded', 'false');
    }
  });
  if (!wasOpen) {
    el.classList.add("open");
    const t = el.querySelector(".submenu-trigger");
    if (t) {
      t.classList.add("open");
      t.setAttribute('aria-expanded', 'true');
    }
  }
}
// Close sub-menus on outside click
document.addEventListener("click", (e) => {
  if (!e.target.closest(".toolbar-submenu")) {
    document.querySelectorAll(".toolbar-submenu.open").forEach(s => {
      s.classList.remove("open");
      const t = s.querySelector(".submenu-trigger");
      if (t) {
        t.classList.remove("open");
        t.setAttribute('aria-expanded', 'false');
      }
    });
  }
});

function updateSubmenuCounts() {
  /* Track whether any count pill's VISIBILITY flipped (display:none ↔
   * inline-block).  That pill is the only thing here that changes the
   * toolbar's natural content width — when it appears, the box must be
   * re-measured or the model name (.ps-label) truncates to fit the stale
   * --toolbar-w.  We reflow ONLY on an actual visibility change so plain
   * recomputes (e.g. depth toggles) stay free. */
  let widthChanged = false;
  const _setCount = (el, count) => {
    if (!el) return;
    el.textContent = count;
    const want = count > 0;
    if (el.classList.contains("visible") !== want) widthChanged = true;
    el.classList.toggle("visible", want);
  };

  // Gate the AI-drawing extra by model availability: hide the whole row when
  //   NO image-gen model is configured (a dead button otherwise). Uses the
  //   registered-model list captured at boot.
  _applyImageGenAvailability();

  // Extras drawer count = every orthogonal capability the user turned on.
  // (Scheduler + Swarm are default tools — no toggles — so they don't count.)
  const extrasCount = (autoTranslate ? 1 : 0) + (humanGuidanceEnabled ? 1 : 0)
    + (browserEnabled ? 1 : 0) + (desktopEnabled ? 1 : 0)
    + (imageGenEnabled ? 1 : 0);
  _setCount(document.getElementById("submenuExtrasCount"), extrasCount);
  const extrasTrigger = document.querySelector("#submenuExtras .submenu-trigger");
  if (extrasTrigger) extrasTrigger.classList.toggle("has-active", extrasCount > 0);

  /* Browser + desktop share ONE merged row (#localControlToggle); its summary
   * badge counts both. Repaint here so the row reflects state restored from a
   * conversation, not just state changed through the modal's switches. */
  if (typeof _lcUpdateBadge === "function") _lcUpdateBadge();

  /* A pill appeared/disappeared → toolbar's intrinsic width shifted by the
   * pill's box.  Re-measure so .ps-label gets its space back. */
  if (widthChanged && typeof _scheduleReflow === "function") _scheduleReflow();
}

/* Hide the AI-drawing toggle(s) when no image-gen model is configured — a
 * button that can't do anything is worse than an absent one. Detection reuses
 * the registered-model list (_registeredModels, populated by
 * _populateModelDropdown from /api/server-config). Best-effort: if the list
 * isn't ready yet we leave the row visible (it re-runs on the next
 * updateSubmenuCounts after config loads). */
function _hasImageGenModel() {
  const models = (typeof _registeredModels !== 'undefined' && _registeredModels) || [];
  for (const m of models) {
    const caps = (m && m.capabilities) || [];
    for (let i = 0; i < caps.length; i++) if (caps[i] === 'image_gen') return true;
  }
  return false;
}
if (typeof window !== 'undefined') runtimeScope._hasImageGenModel = _hasImageGenModel;

function _applyImageGenAvailability() {
  const models = (typeof _registeredModels !== 'undefined' && _registeredModels) || [];
  if (!models.length) return;  // config not loaded yet — don't hide prematurely
  const ok = _hasImageGenModel();
  const ids = ['imageGenToggle', 'imageGenModeBtn', 'mobileImageGenToggle', 'mobileImageGenModeBtn'];
  for (const id of ids) {
    const el = document.getElementById(id);
    if (el) el.style.display = ok ? '' : 'none';
  }
  // If image-gen was somehow enabled but no model exists, turn it off so the
  // wire config never asks for a tool the server can't honor.
  if (!ok && typeof imageGenEnabled !== 'undefined' && imageGenEnabled
      && typeof _applyImageGenToolUI === 'function') {
    _applyImageGenToolUI(false);
  }
}
if (typeof window !== 'undefined') runtimeScope._applyImageGenAvailability = _applyImageGenAvailability;

function cycleSearchMode() {
  const modes = ["off", "multi"];
  const idx = modes.indexOf(searchMode === "single" ? "multi" : searchMode);
  _applySearchModeUI(modes[(idx + 1) % modes.length]);
  captureActiveConversationSettings();
  debugLog(`Search: ${searchMode}`, "success");
}

/* ── Browser bridge ──────────────────────────────────────────────
 * The browser bridge no longer has its own toolbar row or its own setup
 * modal. Both it and the desktop agent are reached through the single
 * "Local Control" entry (#localControlToggle → #localControlModal, see
 * frontend/src/runtime/local-control.js): from the user's side "let Tofu act on my
 * machine" is ONE concept, and two rows + two modals + two status dots was
 * strictly more cognitive load than one.
 *
 * The wire flag `browserEnabled` is unchanged and still independent of
 * `desktopEnabled` — only the surface merged. `_applyBrowserUI` (main.js)
 * remains the single painter and is what the merged modal's switch drives.
 *
 * toggleBrowser() is kept as a thin alias because mobile and extension
 * callers still reach the bridge by name. It opens the merged modal instead
 * of flipping blind. */
function toggleBrowser() {
  if (typeof openLocalControlModal === 'function') {
    openLocalControlModal();
    return;
  }
  // Bundle shipped without local-control.js — degrade to a plain flip rather
  // than making the entry a dead button.
  _applyBrowserUI(!browserEnabled);
  captureActiveConversationSettings();
}
function downloadBrowserExtension() {
  // Carry the browser's OWN base (origin + live BASE_PATH, e.g. /proxy/15000
  // behind a cloud-IDE gateway) so the zip's bridge_preseed pairs the
  // extension with an address this browser demonstrably reaches — a
  // server-side request.host_url loses BOTH the external https scheme and
  // the proxy prefix there, pointing the extension at the gateway's default
  // route (the 2026-08-04 "HTTP 405" incident). The backend pins the param
  // to the request's Host, so only scheme/path are ever adopted from it.
  const base = encodeURIComponent(window.location.origin + BASE_PATH);
  window.open(apiUrl("/api/browser/download?base=" + base), "_blank");
}

/* Chrome 142+ ships "Local Network Access" prompts on by default, which fire
 * per-site during multi-tab searches. The extension can't grant this itself,
 * so when the CONNECTED extension reports Chromium >= 142 we surface guidance
 * to disable the prompt at the browser level (flag or managed policy). */
function _applyBrowserLnaWarning(chromeMajor) {
  const box = document.getElementById("browserLnaWarning");
  if (!box) return;
  if (!chromeMajor || chromeMajor < 142) {
    box.style.display = "none";
    return;
  }
  box.style.display = "";
  // Click-to-copy the policy JSON.
  const pol = document.getElementById("browserLnaPolicy");
  if (pol && !pol._wired) {
    pol._wired = true;
    pol.onclick = function () {
      if (typeof _safeClipboardWrite === "function") {
        _safeClipboardWrite(pol.textContent)
          .then(() => pol.classList.add("copied"))
          .catch(() => {});
      }
    };
  }
  // Show the OS-specific managed-policy directory (best-effort, from the UA of
  // the browser viewing this page — usually the same machine as the bridge).
  const pathEl = document.getElementById("browserLnaPath");
  if (pathEl) {
    const ua = (navigator.userAgent || "").toLowerCase();
    let dir = "";
    if (ua.includes("windows")) {
      dir = "HKLM\\SOFTWARE\\Policies\\Google\\Chrome\\ (via registry / Group Policy)";
    } else if (ua.includes("mac os") || ua.includes("macintosh")) {
      dir = "defaults write com.google.Chrome LocalNetworkAccessAllowedForUrls -array '*'";
    } else {
      dir = "/etc/opt/chrome/policies/managed/tofu-lna.json";
    }
    const label = (typeof t === "function") ? t("browser.lnaPathLabel") : "Place it at:";
    pathEl.style.display = "";
    pathEl.innerHTML = label + " <code>" + dir.replace(/</g, "&lt;") + "</code>";
  }
}

// ══════════════════════════════════════════════════════
// Autopilot (Virtual User auto-replies until VU emits TASK_DONE)
// ══════════════════════════════════════════════════════
function _applyAutopilotUI(enabled) {
  _applyAgentModeUI(enabled ? 'autopilot'
    : resolveAgentMode(planMode, false));
}

/* Turning Autopilot off also clears its durable armed marker. Paint-only
 * restore paths never call this mutating port. */
function _disarmAutopilot() {
  const conv = typeof getActiveConv === 'function' ? getActiveConv() : null;
  if (conv && typeof Api !== 'undefined' && Api.chat?.disarmAutopilot) {
    Api.chat.disarmAutopilot(conv.id).then((response) => {
      if (typeof _applyDisarmResponse === 'function') {
        _applyDisarmResponse(conv.id, response);
      }
      if (typeof _refreshServerQueue === 'function') _refreshServerQueue(conv.id);
    }).catch((error) => console.warn(
      '[Autopilot] disarm failed:', error?.message || error));
  }
}

function toggleAutopilot() {
  return setAgentMode(autopilotEnabled ? 'standard' : 'autopilot');
}

/**
 * Arm autopilot for the active conversation — the explicit "hand it over"
 * gesture (empty send while autopilot is ON).
 *
 * Enqueues a persistent armed-marker (priority 90) into the server-side
 * turn-source queue.  Unlike the old behavior, this works whether or not a
 * reply is currently streaming:
 *   • Streaming    → the in-flight task's config is flipped too, so the VU
 *     takes over at its natural stop without re-sending.
 *   • Idle (done)  → the marker still arms autopilot; it shows in the queue
 *     bar as "Autopilot 待接管" and the user can cancel it.
 * The marker outranks nothing and is outranked by every real message, so a
 * human message the user types later is always processed first.
 *
 * After arming we refresh the queue bar so the cancellable sentinel appears.
 */
function _maybeArmAutopilot() {
  const conv = getActiveConv();
  if (!conv) return;
  if (!(typeof Api !== 'undefined' && Api.chat && Api.chat.armAutopilot)) return;
  Api.chat.armAutopilot(conv.id).then((r) => {
    if (r && r.armed) {
      debugLog("Autopilot armed — virtual user will take over (you can cancel it in the queue bar)", "success");
      if (typeof showToast === "function") {
        showToast("", t('autopilot.armedTitle'), t('autopilot.armedBody'), 4000);
      }
    }
    /* Surface the pending sentinel and dispatchable queue items in the bar. */
    if (typeof _refreshServerQueue === 'function') _refreshServerQueue(conv.id);
  }).catch((e) => console.warn('[Autopilot] arm failed:', e && e.message));
}
if (typeof window !== 'undefined') runtimeScope._maybeArmAutopilot = _maybeArmAutopilot;

/**
 * Kick autopilot on the active conversation when its reply has ALREADY
 * finished — the "push it forward" gesture (empty-Enter, autopilot ON, not
 * streaming). Spawns a backend carrier task that runs the virtual-user hook
 * directly (no AI worker turn), then connects to its SSE stream so the VU
 * bubble streams in identically to a natural-stop takeover.
 *
 * No-op when something is still streaming (the arm path covers that) or when
 * autopilot is off.
 */
async function _kickAutopilot() {
  const conv = getActiveConv();
  if (!conv) return;
  if (typeof autopilotEnabled !== 'undefined' && !autopilotEnabled) return;
  const streaming = typeof convIsBusy === 'function' && convIsBusy(conv);
  if (streaming) return;
  if (!(typeof Api !== 'undefined' && Api.chat && Api.chat.kickAutopilot)) return;
  let cfg = {};
  try {
    if (typeof _buildConvConfig === 'function') cfg = await _buildConvConfig(conv);
  } catch (e) {
    console.warn('[Autopilot] kick: _buildConvConfig failed, using defaults:', e && e.message);
  }
  try {
    const r = await Api.chat.kickAutopilot(conv.id, cfg);
    if (r && r.taskId) {
      debugLog('Autopilot taking over — virtual user is composing the next reply', 'success');
      try {
        await refreshConversationRuntime(conv.id);
      } catch (syncError) {
        console.warn(
          '[Autopilot] kick accepted; authoritative refresh will retry:',
          syncError && syncError.message,
        );
      }
      renderConversationList();
      updateSendButton();
    }
  } catch (e) {
    /* 409 = a task is already running for this conv (arm path applies). */
    console.warn('[Autopilot] kick failed:', e && e.message);
  }
}
if (typeof window !== 'undefined') runtimeScope._kickAutopilot = _kickAutopilot;

// ══════════════════════════════════════════════════════
// Debug-only saved Flow choices inside Agent Mode. Selection is exclusive.
// ══════════════════════════════════════════════════════

function _flowCatalogSnapshot() {
  return typeof _orchestrationFlowCatalog !== 'undefined'
      && _orchestrationFlowCatalog
    ? _orchestrationFlowCatalog.snapshot() : [];
}

function _applyFlowUI(flowVal) {
  /* A builtin Flow remains distinct from standalone Autopilot mode. */
  activeFlow = flowVal || '';
  // Reflect the radio-style selection in the unified dropdown list.
  document.querySelectorAll('#flowMenuList .flow-menu-item').forEach(el => {
    const selected = (el.dataset.flow || '') === activeFlow;
    el.classList.toggle('selected', selected);
    el.setAttribute('aria-selected', selected ? 'true' : 'false');
    el.setAttribute('aria-checked', selected ? 'true' : 'false');
    el.setAttribute('tabindex', selected ? '0' : '-1');
  });
  _paintAgentModeUI();
  if (typeof updateMobileSheet === 'function') updateMobileSheet();
}

function _reconcileActiveFlowCatalog(custom) {
  if (typeof reconcileOrchestrationFlowSelection !== 'function') return false;
  const next = reconcileOrchestrationFlowSelection(activeFlow, custom, true);
  if (next === activeFlow) return false;
  _applyFlowUI(next);
  const conv = typeof getActiveConv === 'function' ? getActiveConv() : null;
  if (conv) {
    conv.activeFlow = next;
    if (typeof scheduleConversationSettingsPersist === 'function') {
      scheduleConversationSettingsPersist(conv);
    }
  }
  if (typeof updateSubmenuCounts === 'function') updateSubmenuCounts();
  if (typeof debugLog === 'function') {
    debugLog(t('toolbar.flowRemoved'), 'warning');
  }
  return true;
}

function _flowDisplayName(flowVal) {
  return typeof orchestrationFlowPickerDisplayName === 'function'
    ? orchestrationFlowPickerDisplayName(
      flowVal, _flowCatalogSnapshot(), t)
    : String(flowVal || '');
}

function _syncActiveFlowLabel() {
  _paintAgentModeUI();
  if (typeof updateMobileSheet === 'function') updateMobileSheet();
}

function setActiveFlow(flowVal) {
  /* Keep builtin flows on the Flow path; do not alias them to Agent Mode. */
  if (_agentInteractionChangeBlocked()) return false;
  const wasAutopilot = autopilotEnabled;
  const nextFlow = String(flowVal || '');
  if (nextFlow) {
    _applyAgentModeUI('standard');
    if (typeof _applyImageGenUI === 'function') _applyImageGenUI(false);
    if (wasAutopilot) _disarmAutopilot();
  }
  _applyFlowUI(nextFlow);
  captureActiveConversationSettings();
  if (typeof updateSubmenuCounts === 'function') updateSubmenuCounts();
  debugLog(
    activeFlow ? `Flow: ${_flowDisplayName(activeFlow)} — runs on the orchestration engine`
               : "Flow: none",
    "success",
  );
  _closeAgentModeMenu();
  return true;
}

function _closeAgentModeMenu() {
  const submenu = document.getElementById('submenuAgentMode');
  if (!submenu || !submenu.classList.contains('open')) return false;
  submenu.classList.remove('open');
  const trigger = document.getElementById('agentModeToggle');
  trigger?.classList.remove('open');
  trigger?.setAttribute('aria-expanded', 'false');
  _flowMenuRenderGeneration++;
  return true;
}

function toggleAgentModeMenu(event) {
  if (event) event.stopPropagation();
  const submenu = document.getElementById('submenuAgentMode');
  if (!submenu) return false;
  const willOpen = !submenu.classList.contains('open');
  if (!willOpen) return _closeAgentModeMenu();
  toggleSubmenu('submenuAgentMode');
  const workflows = document.getElementById('agentWorkflowSection');
  if (workflows && workflows.style.display !== 'none') _populateFlowMenu();
  return true;
}

function handleAgentModeMenuTriggerKey(event) {
  if (!event || (event.key !== 'ArrowDown' && event.key !== 'ArrowUp')) {
    return false;
  }
  event.preventDefault();
  event.stopPropagation();
  const menu = document.getElementById('submenuAgentMode');
  const wasOpen = !!menu?.classList.contains('open');
  if (!wasOpen) toggleAgentModeMenu();
  const rows = document.querySelectorAll(
    '#agentModeMenu [role="radio"]:not(:disabled)');
  if (!rows.length) return false;
  let row = Array.prototype.find.call(rows, candidate =>
    candidate.getAttribute('aria-checked') === 'true');
  if (!row) row = event.key === 'ArrowUp' ? rows[rows.length - 1] : rows[0];
  const focusRow = () => {
    if (!menu?.classList.contains('open') || !row.isConnected || row.disabled) return;
    row.focus();
  };
  const focusRowWhenVisible = (remainingFrameChecks) => {
    if (!menu?.classList.contains('open') || !row.isConnected || row.disabled) return;
    const visibility = typeof window.getComputedStyle === 'function'
      ? window.getComputedStyle(row).visibility
      : 'visible';
    if (visibility !== 'hidden') {
      focusRow();
      return;
    }
    if (remainingFrameChecks > 0) {
      window.requestAnimationFrame(() => {
        focusRowWhenVisible(remainingFrameChecks - 1);
      });
    }
  };
  /* The popover animates visibility for 160 ms. Chromium refuses focus while
   * that transition still computes to hidden, so wait for a paintable frame.
   * Keep the retry bounded in case a future style intentionally stays hidden. */
  if (!wasOpen && typeof window !== 'undefined'
      && typeof window.requestAnimationFrame === 'function') {
    window.requestAnimationFrame(() => { focusRowWhenVisible(30); });
  } else {
    focusRow();
  }
  return true;
}

let _flowMenuRenderGeneration = 0;

/* Compatibility names for old cached markup and extensions. New UI uses the
 * unified Agent Mode menu above. */
function toggleFlowMenu(event) { return toggleAgentModeMenu(event); }
function handleFlowMenuTriggerKey(event) {
  return handleAgentModeMenuTriggerKey(event);
}
function _closeFlowMenu() {
  return _closeAgentModeMenu();
}

function openOrchestrationFromAgentMode() {
  _closeAgentModeMenu();
  if (typeof closeMobileSheet === 'function') closeMobileSheet();
  /* The Studio stylesheet lives in the lazy features/orchestration chunk.
   * Only the feature-bridge stub on runtimeScope loads that chunk; calling
   * the lexical openOrchestration directly mounts the shell unstyled. */
  const lexical = typeof openOrchestration === 'function' ? openOrchestration : null;
  const routed = (typeof runtimeScope !== 'undefined'
      && typeof runtimeScope.openOrchestration === 'function'
      && runtimeScope.openOrchestration !== lexical)
    ? runtimeScope.openOrchestration
    : lexical;
  if (!routed) return false;
  routed();
  return true;
}

function _renderFlowMenu(custom) {
  const list = document.getElementById("flowMenuList");
  if (!list || typeof projectOrchestrationFlowPickerItems !== 'function') {
    return false;
  }
  const focused = list.contains(document.activeElement)
    ? document.activeElement.getAttribute('data-flow') : null;
  const catalog = typeof _orchestrationFlowCatalog !== 'undefined'
    ? _orchestrationFlowCatalog : null;
  let catalogNoticeVisible = false;
  if (typeof renderOrchestrationFlowCatalogNotice === 'function') {
    catalogNoticeVisible = renderOrchestrationFlowCatalogNotice(
      document.getElementById('flowMenuStatus'), catalog, t);
  }
  const items = projectOrchestrationFlowPickerItems(custom, t, {
    includeNone: false,
    includeBuiltins: false,
  });
  list.innerHTML = items.length ? items.map(it =>
    '<button type="button" class="flow-menu-item' + ((it.flow === activeFlow) ? ' selected' : '') + '" '
    + 'role="radio" aria-checked="' + (it.flow === activeFlow ? 'true' : 'false') + '" '
    + 'aria-selected="' + (it.flow === activeFlow ? 'true' : 'false') + '" '
    + 'data-flow="' + escapeHtml(it.flow) + '">'
    + '<span class="flow-menu-icon">' + orchestrationFlowPickerIcon(it.flow) + '</span>'
    + '<span class="flow-menu-text"><span class="flow-menu-name">' + escapeHtml(it.name) + '</span>'
    + '<span class="flow-menu-desc">' + escapeHtml(it.desc) + '</span></span>'
    + '<span class="flow-menu-check">✓</span>'
    + '</button>'
  ).join('') : (catalogNoticeVisible ? ''
    : '<div class="agent-workflow-empty">'
      + escapeHtml(t('toolbar.noSavedWorkflows')) + '</div>');
  if (typeof wireOrchestrationFlowPicker === 'function') {
    wireOrchestrationFlowPicker(list, {
      onSelect: function (flow) {
        setActiveFlow(flow);
        const trigger = document.getElementById('agentModeToggle');
        if (trigger) trigger.focus();
      },
      onEscape: function () {
        if (!_closeAgentModeMenu()) return;
        const trigger = document.getElementById('agentModeToggle');
        if (trigger) trigger.focus();
      },
    });
  }
  if (focused !== null) {
    const rows = list.querySelectorAll('[role="radio"][data-flow]');
    const row = Array.prototype.find.call(rows, candidate =>
      (candidate.getAttribute('data-flow') || '') === focused);
    if (row) row.focus();
  }
  _paintAgentModeUI();
  return true;
}

async function _populateFlowMenu() {
  const list = document.getElementById("flowMenuList");
  if (!list) return;
  // One shared catalogue owns custom-workflow transport, projection,
  // freshness and failure fallback for desktop and mobile.
  const owner = ++_flowMenuRenderGeneration;
  const catalog = typeof _orchestrationFlowCatalog !== 'undefined'
      && _orchestrationFlowCatalog ? _orchestrationFlowCatalog : null;
  const request = catalog ? catalog.load() : Promise.resolve([]);
  _renderFlowMenu(catalog ? catalog.snapshot() : []);
  const custom = await request;
  if (owner !== _flowMenuRenderGeneration) return false;
  return _renderFlowMenu(custom);
}

/* ═══ Folder management UI ═══ */
