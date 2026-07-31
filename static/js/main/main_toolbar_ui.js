/* ═══════════════════════════════════════════════════════════════════
   main toolbar ui — extracted from main.js (split 2026-05-28)

   Toolbar UI: model dropdown, presets, auto-translate, submenu, browser/endpoint/autopilot toggles.

   This file is concatenated by lib/js_bundler.py BEFORE main.js so
   the boot IIFE can reference these symbols. Symbols share `window`
   scope — no imports / exports needed.
   ═══════════════════════════════════════════════════════════════════ */

// ══════════════════════════════════════════════════════
//  Defensive fallback for isChatModel / applyCapabilityTaxonomy
//
//  core/model_caps.js is the SSOT for chat-vs-non-chat classification,
//  and normally loads BEFORE this file. But if the bundle ever ships
//  without it (stale bundler manifest, minifier regression, CDN partial
//  fetch, _BUNDLE_FILES drift), every model picker that calls the
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
  if (typeof window.isChatModel !== 'function') {
    var _set = new Set(_FE_CHAT_EXCLUDED_FALLBACK);
    window.isChatModel = function _isChatModelFallback(m) {
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
  if (typeof window.applyCapabilityTaxonomy !== 'function') {
    // Minimal shim so the /api/server-config ingestion path stays functional
    // even without model_caps.js — swap in the server's chat_excluded_caps
    // if provided. Same behavioural contract as the real one, minus the
    // dispatcher-set bookkeeping (frontend doesn't filter with that anyway).
    window.applyCapabilityTaxonomy = function _applyCapabilityTaxonomyFallback(payload) {
      if (!payload || typeof payload !== 'object') return;
      var xs = payload.chat_excluded_caps;
      if (!Array.isArray(xs) || xs.length === 0) return;
      var _set2 = new Set(xs);
      window.isChatModel = function _isChatModelFallback2(m) {
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
 *  static/js/settings/visibility_defaults.js. */
var _modelCapsMissingWarned = false;
function _warnModelCapsMissing() {
  if (_modelCapsMissingWarned) return;
  _modelCapsMissingWarned = true;
  try { console.warn('[Tofu] isChatModel unavailable — showing all models unfiltered (non-chat models may appear in the picker)'); } catch (_) {}
}
if (typeof window !== 'undefined') window._warnModelCapsMissing = _warnModelCapsMissing;

// ── Toggles ──
function toggleThinking() {
  thinkingEnabled = !thinkingEnabled;
}

// ══════════════════════════════════════════════════════
// ★ Two-tier capability dial (Chat / Studio)
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
if (typeof window !== 'undefined') window._CHAT_MODE_DEFAULTS = _CHAT_MODE_DEFAULTS;

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
if (typeof window !== 'undefined') window._applyChatModeUI = _applyChatModeUI;

/* Open/close the mode popover (upward). Twin of toggleFlowMenu — one popover
 * open at a time; closes on outside click (handler below). */
function toggleChatModeMenu(e) {
  if (e) e.stopPropagation();
  const menu = document.getElementById('chatModeMenu');
  if (!menu) return;
  const willOpen = !menu.classList.contains('open');
  // Close the sibling flow menu so only one popover shows at once.
  const flow = document.getElementById('flowMenu');
  if (flow) flow.classList.remove('open');
  menu.classList.toggle('open', willOpen);
}
if (typeof window !== 'undefined') window.toggleChatModeMenu = toggleChatModeMenu;

function closeChatModeMenu() {
  const menu = document.getElementById('chatModeMenu');
  if (menu) menu.classList.remove('open');
}
if (typeof window !== 'undefined') window.closeChatModeMenu = closeChatModeMenu;

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
    // has-project branch ran _applyChatModeUI + _saveConvToolState BEFORE
    // opening — if either threw synchronously the panel never opened, so an
    // already-attached conv could never change its path (while attaching a
    // fresh one, which skips that bookkeeping, worked). The dial bookkeeping is
    // now best-effort and cannot block the affordance.
    if (typeof openProjectModal === 'function') openProjectModal();
    if (hasProject) {
      try {
        _applyChatModeUI('studio');
        _saveConvToolState();
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
  _saveConvToolState();
  debugLog('Mode: Chat', 'success');
}
if (typeof window !== 'undefined') window.setChatMode = setChatMode;

/* Called by mpApplyFolders after a project is successfully attached — promote
 * the dial to Studio (the tier IS "a project is attached"). Kept separate from
 * setChatMode so the project path owns the promotion. The promotion is
 * persisted immediately — without it conv.chatMode keeps the stale tier until
 * the next unrelated toggle, and a reload in between restores the wrong dial. */
function onProjectAttached() {
  if (chatMode !== 'studio') {
    _applyChatModeUI('studio');
    if (typeof _saveConvToolState === 'function') _saveConvToolState();
  }
}
if (typeof window !== 'undefined') window.onProjectAttached = onProjectAttached;

/* Called by clearProject — a project-less chat is never Studio; fall back to
 * the everyday Chat tier AND persist the fallback. Without the persist,
 * conv.chatMode kept 'studio' with an empty projectPath, and the restore path
 * trusting that stored tier resurrected Studio on the next reload/conv-switch
 * even though no project was attached. */
function onProjectCleared() {
  if (chatMode === 'studio') {
    _applyChatModeUI('chat');
    if (typeof _saveConvToolState === 'function') _saveConvToolState();
  }
}
if (typeof window !== 'undefined') window.onProjectCleared = onProjectCleared;

/* Derive the correct tier from the current atomic flags — used on restore of
 * an OLD conversation that has no stored chatMode (pre-feature convs). With
 * the air/pro merge there are only two tiers: a project ⇒ studio, else chat. */
function _deriveChatModeFromFlags(conv) {
  if (conv && conv.projectPath) return 'studio';
  return 'chat';
}
if (typeof window !== 'undefined') window._deriveChatModeFromFlags = _deriveChatModeFromFlags;
/* ★ Populate model dropdown dynamically from the registered models list.
 * Called once at startup from _loadServerConfigAndPopulate(). */
function _populateModelDropdown(models) {
  /* ★ Write into the inner list container, NOT #presetDropdown itself — the
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
    if (typeof isChatModel !== 'function') { _warnModelCapsMissing(); return true; }
    return isChatModel(m);
  });

  /* Group models by the SHARED brand rule (core/model_group.js) — NOT by
   * provider_id. Grouping by provider leaks the backend's wire detail: the
   * Meituan gateway serves openai on one face and anthropic on another
   * (sankuai vs sankuai_anthropic), which the picker would render as TWO
   * "Meituan" sections. The settings preset tab groups by the same brand
   * rule, so the two lists can never disagree. Degrade to a per-provider
   * grouping only if the shared module failed to load (stale bundle). */
  const _hasGroup = (typeof modelGroupKey === 'function'
                     && typeof modelGroupLabel === 'function');
  const grouped = {};  // groupKey → { name, models: [] }
  for (const m of visibleModels) {
    const _entryProvider = { brand: m.brand, name: m.provider_name };
    const gkey = _hasGroup
      ? modelGroupKey(_entryProvider, m)
      : (m.provider_id || 'default');
    const gname = _hasGroup
      ? modelGroupLabel(gkey, m.provider_name)
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
  for (const gkey of groupKeys) {
    const group = grouped[gkey];
    if (_canSort) group.models.sort(_compareModelsByDisplayName);
    /* Only show section headers when there are multiple groups */
    if (groupKeys.length > 1) {
      const labelDiv = document.createElement('div');
      labelDiv.className = 'ps-dd-section-label';
      labelDiv.textContent = group.name;
      dropdown.appendChild(labelDiv);
    }

    for (const m of group.models) {
      const brand = m.brand || (typeof _detectBrand === 'function' ? _detectBrand(m.model_id) : 'generic');
      const item = document.createElement('div');
      item.className = 'preset-dropdown-item';
      item.setAttribute('data-value', m.model_id);
      item.onclick = function() { selectModel(m.model_id); };
      const isActive = m.model_id === (config.model || serverModel);
      if (isActive) item.classList.add('active');
      /* Brand icon */
      const iconSpan = document.createElement('span');
      iconSpan.className = 'ps-dd-icon';
      if (typeof _brandSvg === 'function') {
        iconSpan.innerHTML = _brandSvg(brand, 14);
      } else {
        iconSpan.textContent = '✦';
      }
      /* Model name label — use friendly short name, not raw model_id */
      const nameSpan = document.createElement('span');
      nameSpan.className = 'ps-dd-label';
      nameSpan.textContent = typeof _modelShortName === 'function' ? _modelShortName(m.model_id) : m.model_id;
      nameSpan.title = m.model_id;
      item.appendChild(iconSpan);
      item.appendChild(nameSpan);
      dropdown.appendChild(item);
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

/* ★ Load the model list from server config and populate the dropdown.
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
        window._uploadShrinkPolicy = data.upload;
      }
      /* Capture context-window policy so the Context Health Bar mirrors the
       * backend exactly. See lib/tasks_pkg/compaction.build_context_policy().
       * Single source of truth — no more frontend-vs-backend limit/threshold
       * drift (the JS table was stuck at 0.82 vs the real 0.90, and a stale
       * per-model regex table). */
      if (data.context && typeof data.context === 'object') {
        window._contextPolicy = data.context;
        if (typeof window.updateContextBar === 'function') window.updateContextBar();
      }
      /* Capture translation policy (stale-partial heuristic threshold) so
       * translation.js mirrors the backend. See lib/text_lang.py. */
      if (data.translation && typeof data.translation === 'object') {
        window._translationPolicy = data.translation;
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
      /* ★ Load IG models now that _hiddenIgModels is populated (avoids race condition
       * where the old setTimeout(2000) could fire before this config fetch completes,
       * causing hidden models to still appear in the IG picker). */
      if (typeof _loadIgModels === 'function') {
        _igModelsLoaded = true;
        _loadIgModels();
      }
      /* ★ Sync serverModel with the configured default model from Settings.
       * Without this, _resetToolsToDefaults() (called on new chat) would always
       * use the hardcoded initial serverModel instead of the user's configured
       * default model from the Settings "默认模型" dropdown. */
      const cfgDefault = data.model_defaults && data.model_defaults.default_model;
      if (cfgDefault) {
        serverModel = cfgDefault;
      }
      _populateModelDropdown(models);

      /* ★ Validate that config.model actually exists among the available models.
       * On fresh deploys (e.g. open-source), config.model may be a hardcoded default
       * (like "aws.claude-opus-4.6") that doesn't exist in the user's provider.
       * If so, fall back to serverModel (from server config) or the first available
       * chat model — pick randomly to avoid always landing on the same one. */
      const chatModels = (models || []).filter(m => {
        if (_hiddenModels.has(m.model_id)) return false;
        if (typeof isChatModel !== 'function') { _warnModelCapsMissing(); return true; }
        return isChatModel(m);
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

      /* ★ Auto-open settings if ?setup=1 (from bootstrap) or no API keys configured */
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

/* ★ Auto-open settings to the API tab if the user just came from bootstrap
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
      if (typeof openSettings === 'function') {
        openSettings();
        // Switch to the API/providers tab
        if (typeof switchSettingsTab === 'function') {
          switchSettingsTab('providers');
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
  try { localStorage.setItem("claude_client_config", JSON.stringify(config)); }
  catch (e) { debugLog(`[selectModel] localStorage save failed: ${e.message}`, 'error'); }
  _saveConvToolState();
  const depthSuffix = _isThinkingCapable(config.model) && config.thinkingDepth
    ? ` [${config.thinkingDepth.toUpperCase()}]`
    : '';
  debugLog(`Model: ${config.model}${depthSuffix}`, "success");
}
// toggleFetch removed — fetch is always on
function toggleCodeExec() {
  _applyCodeExecUI(!codeExecEnabled);
  _saveConvToolState();
  debugLog(`Code Exec: ${codeExecEnabled ? "ON" : "OFF"}`, "success");
}
function toggleAutoTranslate() {
  autoTranslate = !autoTranslate;
  localStorage.setItem("claude_auto_translate", JSON.stringify(autoTranslate));
  const btn = document.getElementById("translateToggle");
  const badge = document.getElementById("translateBadge");
  if (btn) btn.classList.toggle("active", autoTranslate);
  if (badge) badge.style.display = autoTranslate ? "" : "none";
  _saveConvToolState();
  debugLog(`Auto-Translate: ${autoTranslate ? "ON" : "OFF"}`, "success");

  // ★ One-time hint about <notranslate> when first enabling
  if (autoTranslate && !localStorage.getItem("claude_translate_hint_shown")) {
    localStorage.setItem("claude_translate_hint_shown", "1");
    showToast(
      "", "Translation Tip",
      "Select text and press Ctrl+Shift+K to wrap it in &lt;notranslate&gt; — that part won't be translated.",
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
// ★ Toolbar Sub-menus — dropdown grouping for tool toggles
// ══════════════════════════════════════════════════════
function toggleSubmenu(id) {
  const el = document.getElementById(id);
  if (!el) return;
  const wasOpen = el.classList.contains("open");
  // close all sub-menus first
  document.querySelectorAll(".toolbar-submenu.open").forEach(s => {
    s.classList.remove("open");
    const t = s.querySelector(".submenu-trigger");
    if (t) t.classList.remove("open");
  });
  if (!wasOpen) {
    el.classList.add("open");
    const t = el.querySelector(".submenu-trigger");
    if (t) t.classList.add("open");
  }
}
// Close sub-menus on outside click
document.addEventListener("click", (e) => {
  if (!e.target.closest(".toolbar-submenu")) {
    document.querySelectorAll(".toolbar-submenu.open").forEach(s => {
      s.classList.remove("open");
      const t = s.querySelector(".submenu-trigger");
      if (t) t.classList.remove("open");
    });
  }
});

function updateSubmenuCounts() {
  /* ★ Track whether any count pill's VISIBILITY flipped (display:none ↔
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

  // ★ Gate the AI-drawing extra by model availability: hide the whole row when
  //   NO image-gen model is configured (a dead button otherwise). Uses the
  //   registered-model list captured at boot.
  _applyImageGenAvailability();

  // Extras drawer count = every orthogonal capability the user turned on.
  // (Scheduler is a default tool — no toggle — so it doesn't count.)
  const extrasCount = (autoTranslate ? 1 : 0) + (humanGuidanceEnabled ? 1 : 0)
    + (browserEnabled ? 1 : 0) + (desktopEnabled ? 1 : 0)
    + (imageGenEnabled ? 1 : 0) + (swarmEnabled ? 1 : 0)
    + (endpointEnabled ? 1 : 0) + (autopilotEnabled ? 1 : 0);
  _setCount(document.getElementById("submenuExtrasCount"), extrasCount);
  const extrasTrigger = document.querySelector("#submenuExtras .submenu-trigger");
  if (extrasTrigger) extrasTrigger.classList.toggle("has-active", extrasCount > 0);

  /* Browser + desktop share ONE merged row (#localControlToggle); its summary
   * badge counts both. Repaint here so the row reflects state restored from a
   * conversation, not just state changed through the modal's switches. */
  if (typeof _lcUpdateBadge === "function") _lcUpdateBadge();

  // Flow: standalone box — no count pill, just reflect active-state on the trigger.
  const flowTrigger = document.getElementById("flowToggle");
  if (flowTrigger) flowTrigger.classList.toggle("has-active", !!activeFlow);

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
if (typeof window !== 'undefined') window._hasImageGenModel = _hasImageGenModel;

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
if (typeof window !== 'undefined') window._applyImageGenAvailability = _applyImageGenAvailability;

function cycleSearchMode() {
  const modes = ["off", "multi"];
  const idx = modes.indexOf(searchMode === "single" ? "multi" : searchMode);
  _applySearchModeUI(modes[(idx + 1) % modes.length]);
  _saveConvToolState();
  debugLog(`Search: ${searchMode}`, "success");
}

/* ── Browser bridge ──────────────────────────────────────────────
 * The browser bridge no longer has its own toolbar row or its own setup
 * modal. Both it and the desktop agent are reached through the single
 * "Local Control" entry (#localControlToggle → #localControlModal, see
 * static/js/local-control.js): from the user's side "let Tofu act on my
 * machine" is ONE concept, and two rows + two modals + two status dots was
 * strictly more cognitive load than one.
 *
 * The wire flag `browserEnabled` is unchanged and still independent of
 * `desktopEnabled` — only the surface merged. `_applyBrowserUI` (main.js)
 * remains the single painter and is what the merged modal's switch drives.
 *
 * toggleBrowser() is kept as a thin alias because callers outside the
 * toolbar reach the bridge by name (toolset-apply.js's revert families,
 * mobile flows). It now opens the merged modal instead of flipping blind. */
function toggleBrowser() {
  if (typeof openLocalControlModal === 'function') {
    openLocalControlModal();
    return;
  }
  // Bundle shipped without local-control.js — degrade to a plain flip rather
  // than making the entry a dead button.
  _applyBrowserUI(!browserEnabled);
  _saveConvToolState();
}
function downloadBrowserExtension() {
  window.open(apiUrl("/api/browser/download"), "_blank");
}

/* ★ Chrome 142+ ships "Local Network Access" prompts on by default, which fire
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
// ★ Agent Swarm
// ══════════════════════════════════════════════════════
function _applySwarmUI(enabled) {
  swarmEnabled = !!enabled;
  document
    .getElementById("swarmToggle")
    ?.classList.toggle("active", swarmEnabled);
  const badge = document.getElementById("swarmBadge");
  if (badge) badge.style.display = swarmEnabled ? "" : "none";
  /* Swarm (execution strategy) is orthogonal to endpoint (review loop) — both can coexist */
}
function toggleSwarm() {
  _applySwarmUI(!swarmEnabled);
  _saveConvToolState();
  debugLog(
    `Agent Swarm: ${swarmEnabled ? "ON — complex tasks will be decomposed into parallel sub-agents" : "OFF"}`,
    "success"
  );
}

// ══════════════════════════════════════════════════════
// ★ Endpoint Mode (Autonomous AI with Self-Review)
// ══════════════════════════════════════════════════════
function _applyEndpointUI(enabled) {
  endpointEnabled = !!enabled;
  const btn = document.getElementById("endpointToggle");
  if (btn) btn.classList.toggle("active", endpointEnabled);
  const badge = document.getElementById("endpointBadge");
  if (badge) badge.style.display = endpointEnabled ? "" : "none";
  /* Endpoint (review loop) is orthogonal to swarm (execution strategy) — both can coexist */
}
function toggleEndpoint() {
  _applyEndpointUI(!endpointEnabled);
  /* Endpoint and Autopilot share the same "model stopped → loop again"
   * boundary; running both at once would double-loop.  Endpoint wins. */
  if (endpointEnabled && autopilotEnabled) {
    _applyAutopilotUI(false);
    debugLog("Autopilot disabled — Endpoint Mode takes precedence", "info");
  }
  if (endpointEnabled && activeFlow) {
    _applyFlowUI('');
    debugLog("Flow cleared — Endpoint Mode takes precedence", "info");
  }
  _saveConvToolState();
  debugLog(
    endpointEnabled
      ? "Endpoint Mode: ON — Planner → Worker → Critic autonomous loop (max 10 iterations)"
      : "Endpoint Mode: OFF",
    "success",
  );
}

// ══════════════════════════════════════════════════════
// ★ Autopilot (Virtual User auto-replies until VU emits TASK_DONE)
// ══════════════════════════════════════════════════════
function _applyAutopilotUI(enabled) {
  autopilotEnabled = !!enabled;
  const btn = document.getElementById("autopilotToggle");
  if (btn) btn.classList.toggle("active", autopilotEnabled);
  const badge = document.getElementById("autopilotBadge");
  if (badge) badge.style.display = autopilotEnabled ? "" : "none";
}
function toggleAutopilot() {
  _applyAutopilotUI(!autopilotEnabled);
  /* Mutual exclusion with endpoint mode — see toggleEndpoint() comment. */
  if (autopilotEnabled && endpointEnabled) {
    _applyEndpointUI(false);
    debugLog("Endpoint Mode disabled — Autopilot takes precedence", "info");
  }
  /* A flow selection and the toggles are mutually exclusive — the flow IS
   * the execution mode (backend drops the toggles when a flow is set). */
  if (autopilotEnabled && activeFlow) {
    _applyFlowUI('');
    debugLog("Flow cleared — Autopilot takes precedence", "info");
  }
  _saveConvToolState();
  debugLog(
    autopilotEnabled
      ? "Autopilot: ON — send an empty message to hand the conversation to the virtual user"
      : "Autopilot: OFF",
    "success",
  );
  /* ★ Turning the toggle ON does NOT take over immediately — opening the
   * switch mid-reply only ENABLES autopilot; the user explicitly hands off by
   * sending an empty message (see _doSendOrGenerate → _maybeArmAutopilot),
   * which enqueues a cancellable armed-marker that the user can see and cancel.
   * Turning the toggle OFF disarms: clears the marker + flips any live task's
   * config off so the loop stops at the current turn's natural end. */
  if (!autopilotEnabled) {
    const _conv = (typeof getActiveConv === 'function') ? getActiveConv() : null;
    if (_conv && typeof Api !== 'undefined' && Api.chat && Api.chat.disarmAutopilot) {
      Api.chat.disarmAutopilot(_conv.id)
        .then((resp) => {
          /* Fold the just-concluded run instantly even with no live stream. */
          if (typeof _applyDisarmResponse === 'function') _applyDisarmResponse(_conv.id, resp);
          if (typeof _refreshServerQueue === 'function') _refreshServerQueue(_conv.id);
        })
        .catch((e) => console.warn('[Autopilot] disarm failed:', e && e.message));
    }
  }
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
    /* Surface the pending sentinel (and any real queued messages) in the bar. */
    if (typeof _refreshServerQueue === 'function') _refreshServerQueue(conv.id);
  }).catch((e) => console.warn('[Autopilot] arm failed:', e && e.message));
}
if (typeof window !== 'undefined') window._maybeArmAutopilot = _maybeArmAutopilot;

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
  const streaming = activeStreams.has(conv.id) || !!conv.activeTaskId;
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
      conv.activeTaskId = r.taskId;
      if (typeof saveConversations === 'function') saveConversations(conv.id);
      renderConversationList();
      updateSendButton();
      debugLog('Autopilot taking over — virtual user is composing the next reply', 'success');
      connectToTask(conv.id, r.taskId, 0, { autopilotKick: true });
    }
  } catch (e) {
    /* 409 = a task is already running for this conv (arm path applies). */
    console.warn('[Autopilot] kick failed:', e && e.message);
  }
}
if (typeof window !== 'undefined') window._kickAutopilot = _kickAutopilot;

// ══════════════════════════════════════════════════════
// ★ Orchestration Flow selector (Mode dropdown)
//   activeFlow ∈ { '' , 'builtin:endpoint', 'builtin:autopilot', <orchId> }.
//   Selecting a flow makes the whole conversation run on the FlowExecutor
//   engine (routes/chat.py → resolve_chat_flow_entry); it is mutually
//   exclusive with the endpoint/autopilot toggles (the flow IS the mode).
// ══════════════════════════════════════════════════════
var _orchFlowCache = null;   // cached [{id,name}] of stored custom flows

function _applyFlowUI(flowVal) {
  /* ★ builtin:autopilot is a REAL engine flow selection (symmetric with
   *   builtin:endpoint) — the "编排流程 → 自动驾驶" dropdown runs the FlowExecutor
   *   autopilot (worker⇄VU) graph, distinct from the "模式" toggle which runs
   *   the live standalone autopilot loop. So restore/sync it as-is like any
   *   other flow; do NOT redirect it to the autopilot toggle. */
  activeFlow = flowVal || '';
  const btn = document.getElementById("flowToggle");
  if (btn) btn.classList.toggle("active", !!activeFlow);
  const badge = document.getElementById("flowBadge");
  if (badge) badge.style.display = activeFlow ? "" : "none";
  const label = document.getElementById("flowActiveLabel");
  if (label) {
    if (activeFlow) {
      label.textContent = _flowDisplayName(activeFlow);
      label.classList.add("visible");
    } else {
      label.textContent = "";
      label.classList.remove("visible");
    }
  }
  // Reflect the radio-style selection in the dropdown list.
  document.querySelectorAll('#flowMenuList .flow-menu-item').forEach(el => {
    el.classList.toggle('selected', (el.dataset.flow || '') === activeFlow);
  });
}

function _flowDisplayName(flowVal) {
  if (!flowVal) return t('toolbar.flowNone');
  if (flowVal === 'builtin:endpoint') return t('toolbar.autonomousMode');
  if (flowVal === 'builtin:autopilot') return t('toolbar.autopilot');
  const f = (_orchFlowCache || []).find(x => ('' + x.id) === flowVal);
  return f ? f.name : t('toolbar.flowCustom');
}

function setActiveFlow(flowVal) {
  /* ★ builtin:autopilot is a REAL engine flow, symmetric with builtin:endpoint:
   *   selecting it runs the FlowExecutor autopilot (worker⇄VU) graph so engine
   *   behavior is observable in the frontend — deliberately DIFFERENT from the
   *   "模式" Autopilot toggle, which runs the live standalone loop. Handle it on
   *   the normal flow path below (no alias to the toggle). */
  _applyFlowUI(flowVal || '');
  /* Flow ⇄ toggles mutual exclusion: a flow owns the loop boundary. */
  if (activeFlow && (endpointEnabled || autopilotEnabled)) {
    _applyEndpointUI(false);
    _applyAutopilotUI(false);
  }
  _saveConvToolState();
  if (typeof updateSubmenuCounts === 'function') updateSubmenuCounts();
  debugLog(
    activeFlow ? `Flow: ${_flowDisplayName(activeFlow)} — runs on the orchestration engine`
               : "Flow: none",
    "success",
  );
  // Close the dropdown after a pick.
  const menu = document.getElementById("flowMenu");
  if (menu) menu.classList.remove("open");
}

function toggleFlowMenu(e) {
  if (e) e.stopPropagation();
  const menu = document.getElementById("flowMenu");
  if (!menu) return;
  const willOpen = !menu.classList.contains("open");
  menu.classList.toggle("open", willOpen);
  if (willOpen) _populateFlowMenu();
}

async function _populateFlowMenu() {
  const list = document.getElementById("flowMenuList");
  if (!list) return;
  // Built-ins are always present; custom flows come from the store.
  let custom = [];
  try {
    custom = await Api.orchestrations.list();
    _orchFlowCache = (custom || []).map(e => ({ id: e.id, name: e.name || 'Untitled' }));
  } catch (err) {
    console.warn('[Flow] list failed:', err && err.message);
    _orchFlowCache = _orchFlowCache || [];
  }
  const items = [
    { flow: '', name: t('toolbar.flowNone'), desc: t('toolbar.flowNoneDesc') },
    { flow: 'builtin:endpoint', name: t('toolbar.autonomousMode'), desc: t('toolbar.autonomousModeDesc') },
    { flow: 'builtin:autopilot', name: t('toolbar.autopilot'), desc: t('toolbar.autopilotDesc') },
  ];
  for (const f of (_orchFlowCache || [])) {
    items.push({ flow: '' + f.id, name: f.name, desc: t('toolbar.flowCustomDesc') });
  }
  list.innerHTML = items.map(it =>
    '<div class="flow-menu-item' + ((it.flow === activeFlow) ? ' selected' : '') + '" '
    + 'data-flow="' + escapeHtml(it.flow) + '" onclick="setActiveFlow(\'' + escapeHtml(it.flow).replace(/'/g, "\\'") + '\')">'
    + '<span class="flow-menu-icon">' + _flowMenuIcon(it.flow) + '</span>'
    + '<span class="flow-menu-text"><span class="flow-menu-name">' + escapeHtml(it.name) + '</span>'
    + '<span class="flow-menu-desc">' + escapeHtml(it.desc) + '</span></span>'
    + '<span class="flow-menu-check">✓</span>'
    + '</div>'
  ).join('');
}

/* SVG icon for a flow-menu row (§3.4 — SVG only, never emoji). The two
 * builtins reuse the EXACT glyphs from their Mode-menu toggles (endpointToggle
 * / autopilotToggle) so the two surfaces read as the same capability; "none"
 * (plain chat) is a speech bubble; any custom Studio flow is a node-graph. */
function _flowMenuIcon(flow) {
  const SW = 'fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"';
  if (flow === 'builtin:endpoint') {
    return '<svg width="16" height="16" viewBox="0 0 24 24" ' + SW + '><path d="M12 2v4"/><path d="M12 18v4"/><path d="M4.93 4.93l2.83 2.83"/><path d="M16.24 16.24l2.83 2.83"/><path d="M2 12h4"/><path d="M18 12h4"/><path d="M4.93 19.07l2.83-2.83"/><path d="M16.24 7.76l2.83-2.83"/><circle cx="12" cy="12" r="4"/></svg>';
  }
  if (flow === 'builtin:autopilot') {
    return '<svg width="16" height="16" viewBox="0 0 24 24" ' + SW + '><circle cx="12" cy="12" r="3"/><path d="M12 1v6m0 10v6m11-11h-6m-10 0H1m17.66-6.34l-4.24 4.24m-5.66 5.66l-4.24 4.24m12.14 0l-4.24-4.24m-5.66-5.66L4.34 4.34"/></svg>';
  }
  if (!flow) {
    // "none" — plain conversation (no engine flow)
    return '<svg width="16" height="16" viewBox="0 0 24 24" ' + SW + '><path d="M21 15a2 2 0 0 1-2 2H7l-4 4V5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2z"/></svg>';
  }
  // A stored custom Studio flow — node graph
  return '<svg width="16" height="16" viewBox="0 0 24 24" ' + SW + '><circle cx="18" cy="5" r="3"/><circle cx="6" cy="12" r="3"/><circle cx="18" cy="19" r="3"/><line x1="8.59" y1="13.51" x2="15.42" y2="17.49"/><line x1="15.41" y1="6.51" x2="8.59" y2="10.49"/></svg>';
}

// Close the flow menu on outside click.
document.addEventListener("click", (e) => {
  if (!e.target.closest("#flowMenuWrapper")) {
    const menu = document.getElementById("flowMenu");
    if (menu) menu.classList.remove("open");
  }
});

/* ═══ Folder management UI ═══ */

