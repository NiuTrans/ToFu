/* ═══════════════════════════════════════════
   main.js — Chat Core, Toolbar, Init
   Orchestrator: sends messages, manages conversations,
   wires toolbar UI, and boots the app.
   Feature modules live in separate files:
     image-gen.js, log-clean.js, translation.js,
     upload.js, project.js, memory.js, scheduler.js, myday.js
   ═══════════════════════════════════════════ */

/* pendingPdfTexts → defined in upload.js */
/* _pendingLogClean → defined in log-clean.js */

/* ── Race-condition guard: incremented on every send or conversation switch ── */
let _sendGeneration = 0;

/* ═══════════════════════════════════════════
   Agent Backend Selection
   ═══════════════════════════════════════════ */
let activeAgentBackend = 'builtin';       // 'builtin' | 'claude-code' | 'codex'
let _agentBackendCapabilities = null;     // BackendCapabilities from server
let _agentBackendCache = null;            // Cached /api/agent-backends/status result

/**
 * Fetch backend status from server and cache it.
 * @returns {Promise<Array>} List of backend info objects.
 */
async function _fetchAgentBackends() {
  try {
    const resp = await fetch(apiUrl('/api/agent-backends/status'));
    if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
    const data = await resp.json();
    _agentBackendCache = data.backends || [];
    return _agentBackendCache;
  } catch (e) {
    console.error('[AgentBackends] Failed to fetch status:', e);
    return _agentBackendCache || [];
  }
}

/**
 * Switch the active agent backend.
 * @param {string} backendName - 'builtin', 'claude-code', or 'codex'
 */
async function switchAgentBackend(backendName) {
  if (backendName === activeAgentBackend) return;

  // Validate availability
  const backends = _agentBackendCache || await _fetchAgentBackends();
  const backend = backends.find(b => b.name === backendName);
  if (!backend) {
    if (typeof debugLog === 'function') debugLog(`Unknown backend: ${backendName}`, 'error');
    return;
  }
  if (!backend.available) {
    if (typeof debugLog === 'function')
      debugLog(`${backend.displayName || backendName} is not installed`, 'error');
    return;
  }
  if (!backend.authenticated) {
    if (typeof debugLog === 'function')
      debugLog(`${backend.displayName || backendName} is not authenticated. Run the CLI and log in first.`, 'error');
    return;
  }

  activeAgentBackend = backendName;
  _agentBackendCapabilities = backend.capabilities || {};

  // Update UI
  _applyAgentBackendUI();
  _applyBackendCapabilities();
  _saveConvToolState();

  if (typeof debugLog === 'function')
    debugLog(`Switched to ${backend.displayName || backendName}`, 'success');
}

/**
 * Update the backend selector button states.
 */
function _applyAgentBackendUI() {
  document.querySelectorAll('.agent-backend-btn').forEach(btn => {
    btn.classList.toggle('active', btn.dataset.backend === activeAgentBackend);
  });
  // Show backend indicator in header
  const indicator = document.getElementById('backendIndicator');
  if (indicator) {
    if (activeAgentBackend === 'builtin') {
      indicator.style.display = 'none';
    } else {
      const backends = _agentBackendCache || [];
      const b = backends.find(x => x.name === activeAgentBackend);
      indicator.textContent = b ? b.displayName : activeAgentBackend;
      indicator.style.display = 'inline-block';
    }
  }
}

/**
 * Show/hide/grey UI controls based on backend capabilities.
 * When using an external backend, Tofu-only features are greyed out
 * and config controls the backend handles itself are hidden.
 */
function _applyBackendCapabilities() {
  const caps = _agentBackendCapabilities || {};
  const isExternal = activeAgentBackend !== 'builtin';

  // Model selector — hide when external backend handles it
  const modelGroup = document.getElementById('modelGroup');
  if (modelGroup) modelGroup.style.display = caps.modelSelector === false ? 'none' : '';
  // Thinking depth — only HIDE when external backend explicitly disables it.
  // Do NOT set display='' here — that clobbers _applyModelUI's inline display:flex,
  // reverting to the CSS default (display:none) and the perf cache in _applyModelUI
  // won't re-apply it since it thinks nothing changed.
  // When switching BACK from an external backend, invalidate the perf cache so
  // _applyModelUI re-applies the depth bar's correct visibility on its next call.
  const depthBar = document.getElementById('thinkingDepthSection');
  if (caps.thinkingDepth === false) {
    if (depthBar) depthBar.style.display = 'none';
  } else if (depthBar && depthBar.style.display === 'none') {
    // Depth bar was hidden by a previous external backend — invalidate the perf cache
    // so the next _applyModelUI call (from _resetToolsToDefaults) does a full DOM update.
    _lastAppliedModelId = null;
  }

  // Search toggle — hide when external backend has its own search
  const searchToggle = document.getElementById('searchModeToggle');
  if (searchToggle) searchToggle.style.display = caps.searchToggle === false ? 'none' : '';

  // Preset selector — hide for external backends
  const presetSelect = document.getElementById('presetSelect');
  if (presetSelect) {
    const presetParent = presetSelect.closest('.preset-group') || presetSelect.parentElement;
    if (presetParent) presetParent.style.display = caps.presetSelector === false ? 'none' : '';
  }

  // Tofu-only feature toggles — grey out when unavailable
  const _toggleMap = {
    'browserToggle':  caps.hasBrowserExt !== false,
    'desktopToggle':  caps.hasDesktopAgent !== false,
    'imageGenToggle': caps.hasImageGen !== false,
    'swarmToggle':    caps.hasSwarm !== false,
    'schedulerToggle': caps.hasScheduler !== false,
    'humanGuidanceToggle': caps.hasHumanGuidance !== false,
  };
  for (const [id, enabled] of Object.entries(_toggleMap)) {
    const el = document.getElementById(id);
    if (el) {
      el.style.opacity = enabled ? '' : '0.35';
      el.style.pointerEvents = enabled ? '' : 'none';
      if (!enabled) el.classList.remove('active');
    }
  }

  // Endpoint mode — hide if not supported
  const endpointToggle = document.getElementById('endpointToggle');
  if (endpointToggle) {
    endpointToggle.style.opacity = caps.endpointMode !== false ? '' : '0.35';
    endpointToggle.style.pointerEvents = caps.endpointMode !== false ? '' : 'none';
  }
}

/** Toggle the agent backend dropdown visibility. */
function _toggleBackendDropdown() {
  const dd = document.getElementById('agentBackendDropdown');
  if (!dd) return;
  const isVisible = dd.style.display !== 'none';
  dd.style.display = isVisible ? 'none' : 'block';

  // Fetch fresh status when opening
  if (!isVisible) {
    _refreshBackendStatuses();
  }

  // Close dropdown when clicking outside
  if (!isVisible) {
    const _closeHandler = (e) => {
      if (!dd.contains(e.target) && e.target.id !== 'agentBackendTrigger' &&
          !e.target.closest('.agent-backend-trigger')) {
        dd.style.display = 'none';
        document.removeEventListener('click', _closeHandler);
      }
    };
    // Delay adding the listener so this click doesn't immediately close it
    setTimeout(() => document.addEventListener('click', _closeHandler), 0);
  }
}

/** Refresh backend status indicators in the dropdown. */
async function _refreshBackendStatuses() {
  const backends = await _fetchAgentBackends();
  for (const b of backends) {
    if (b.name === 'builtin') continue;

    const statusEl = b.name === 'claude-code'
      ? document.getElementById('ccStatus')
      : document.getElementById('codexStatus');
    const iconEl = b.name === 'claude-code'
      ? document.getElementById('ccStatusIcon')
      : document.getElementById('codexStatusIcon');
    const btnEl = document.querySelector(`.agent-backend-btn[data-backend="${b.name}"]`);

    if (statusEl) {
      if (!b.available) {
        statusEl.textContent = 'Not installed';
        statusEl.style.color = 'var(--text-tertiary)';
      } else if (!b.authenticated) {
        statusEl.textContent = 'Not authenticated';
        statusEl.style.color = '#f59e0b';
      } else {
        statusEl.textContent = b.version || 'Ready';
        statusEl.style.color = '#22c55e';
      }
    }
    if (iconEl) {
      if (!b.available) {
        iconEl.textContent = '✗';
        iconEl.className = 'ab-status ab-unavailable';
      } else if (!b.authenticated) {
        iconEl.textContent = '!';
        iconEl.className = 'ab-status ab-not-auth';
      } else {
        iconEl.textContent = '✓';
        iconEl.className = 'ab-status ab-ready';
      }
    }
    if (btnEl) {
      btnEl.disabled = !b.available || !b.authenticated;
    }
  }
}

// ── Conversation CRUD ──
function _purgeEmptyConvs() {
  const before = conversations.length;
  const purged = [];
  conversations = conversations.filter((c) => {
    const keep = c.messages.length > 0 || c.id === activeConvId || (c._serverMsgCount || 0) > 0 || c._needsLoad;
    if (!keep) purged.push(`${c.id.slice(0,8)}(msgs=${c.messages.length},srv=${c._serverMsgCount||0},load=${!!c._needsLoad})`);
    return keep;
  });
  if (purged.length > 0) {
    console.warn(`[_purgeEmptyConvs] Purged ${purged.length} empty convs: ${purged.join(', ')}`);
  }
}
// ── Per-conversation tool state helpers ──
/* ── Brand detection for model_id — reuse _detectBrand from settings.js ── */
const _DEPTH_ICONS  = { off: '', medium: '', high: '', xhigh: '', max: '' };
const _DEPTH_ICON_FALLBACK = '';
const _DEPTH_LABELS = { off: 'Off', medium: 'Med', high: 'High', xhigh: 'xHigh', max: 'Max' };
/* Models whose model_id indicates thinking/depth support.
 * Uses server-provided thinking_default from _registeredModels;
 * falls back to regex before server config loads. */
function _isThinkingCapable(modelId) {
  if (_registeredModels.length > 0) {
    const reg = _registeredModels.find(m => m.model_id === modelId);
    if (reg) return !!reg.thinking_default;
  }
  // Fallback regex before server config loads
  return /claude|opus|sonnet|gemini|qwen|doubao|minimax|deepseek/i.test(modelId);
}
/* ★ Registered model list — populated from /api/server-config at startup */
let _registeredModels = [];   // [{ model_id, brand, thinking_default, capabilities }]
/* ★ Hidden models — loaded from server config, not shown in dropdown */
let _hiddenModels = new Set();
/* ★ Hidden image gen models — loaded from server config, not shown in image gen picker */
var _hiddenIgModels = new Set();  // shared with image-gen.js

/* _modelShortName is defined in settings.js (loaded earlier) */

/* ★ Track what _applyModelUI last applied so we can skip redundant work */
let _lastAppliedModelId = null;
let _lastAppliedIsThinking = null;

function _applyModelUI(modelId) {
  if (!modelId) modelId = config.model || serverModel;
  /* Legacy preset migration */
  if (typeof _LEGACY_PRESET_TO_MODEL !== 'undefined' && _LEGACY_PRESET_TO_MODEL[modelId]) {
    modelId = _LEGACY_PRESET_TO_MODEL[modelId];
  }
  config.model = modelId;
  const brand = typeof _detectBrand === 'function' ? _detectBrand(modelId) : 'generic';
  const shortName = _modelShortName(modelId);
  const isThinking = _isThinkingCapable(modelId);
  /* ★ Ensure thinkingDepth is always set for thinking models, null for non-thinking.
   * This prevents the || "medium" fallback from leaking depth to non-thinking models. */
  if (isThinking) {
    config.thinkingDepth = config.thinkingDepth || config.defaultThinkingDepth;
  } else {
    config.thinkingDepth = null;
  }
  const depth = config.thinkingDepth || config.defaultThinkingDepth;
  /* ★ Set thinkingEnabled based on depth: 'off' disables thinking even for thinking-capable models */
  thinkingEnabled = isThinking && depth !== 'off';

  /* ★ PERF: Skip all DOM work + reflow if model hasn't actually changed.
   * This is the common case when switching between conversations that use
   * the same model (e.g. the default model). Saves:
   *   - querySelectorAll(".preset-dropdown-item").forEach (N items)
   *   - _scheduleReflow → _reflowToolbar (toolbar width recalc)
   *   - depth bar show/hide DOM manipulation
   * Still update config.model / config.thinkingDepth above so state is correct. */
  const thinkingChanged = _lastAppliedIsThinking !== isThinking;
  if (_lastAppliedModelId === modelId && !thinkingChanged) {
    /* Model unchanged, but depth might have changed — just update badges */
    _updateDepthButtons(depth);
    const modelBadge = document.getElementById("modelBadge");
    if (modelBadge) {
      if (isThinking && depth !== 'off') {
        modelBadge.innerHTML = `${shortName} &middot; ${_DEPTH_ICONS[depth] || _DEPTH_ICON_FALLBACK} ${_DEPTH_LABELS[depth] || depth}`;
      } else {
        modelBadge.textContent = shortName;
      }
    }
    return;
  }
  _lastAppliedModelId = modelId;
  _lastAppliedIsThinking = isThinking;

  // ★ Update model badge
  const modelBadge = document.getElementById("modelBadge");
  if (modelBadge) {
    if (isThinking && depth !== 'off') {
      modelBadge.innerHTML = `${shortName} &middot; ${_DEPTH_ICONS[depth] || _DEPTH_ICON_FALLBACK} ${_DEPTH_LABELS[depth] || depth}`;
    } else {
      modelBadge.textContent = shortName;
    }
  }

  // ★ Update toggle button
  const toggle = document.getElementById("presetToggle");
  if (toggle) {
    toggle.setAttribute("data-model", modelId);
    toggle.setAttribute("data-brand", brand);
    const iconEl = toggle.querySelector(".ps-icon");
    const labelEl = toggle.querySelector(".ps-label");
    if (labelEl) {
      /* ★ Don't show depth in toggle label — the depth bar buttons right next to it
       * already show which depth is active. Removing the suffix keeps the toggle compact. */
      labelEl.textContent = shortName;
    }
    if (iconEl) {
      if (brand !== 'generic' && typeof _brandSvg === 'function') {
        iconEl.innerHTML = _brandSvg(brand, 12);
      } else if (isThinking) {
        iconEl.innerHTML = _DEPTH_ICONS[depth] || _DEPTH_ICON_FALLBACK;
      } else {
        iconEl.textContent = '';
      }
    }
  }

  // ★ Highlight active model item in dropdown
  document.querySelectorAll(".preset-dropdown-item").forEach((item) => {
    item.classList.toggle("active", item.getAttribute("data-value") === modelId);
  });
  _updateDepthButtons(depth);

  // ★ Show/hide thinking-depth bar
  const depthBar = document.getElementById("thinkingDepthSection");
  const modelGroup = document.getElementById("modelGroup");
  if (depthBar) {
    /* ★ PERF FIX: Suppress the depth bar's CSS opacity transition during
     * programmatic show/hide (conv switches, model changes).  The .2s fade
     * causes the bar to animate sluggishly ("stuck with glue") instead of
     * snapping instantly.
     * Fix: disable transition → apply state → flush layout → restore. */
    depthBar.style.transition = 'none';
    if (isThinking) {
      depthBar.style.display = 'flex';
      depthBar.style.opacity = '1';
      depthBar.style.pointerEvents = 'auto';
      modelGroup?.classList.remove('depth-hidden');
    } else {
      depthBar.style.opacity = '0';
      depthBar.style.pointerEvents = 'none';
      modelGroup?.classList.add('depth-hidden');
      depthBar.style.display = 'none';  /* instant hide — no setTimeout delay */
    }
    depthBar.offsetWidth; /* flush layout with transition:none */
    depthBar.style.transition = ''; /* restore CSS transition for hover effects etc. */
  }
  document.getElementById("presetWrapper")?.classList.remove("open");
  /* ★ Resize .input-inner to fit toolbar content — must run after DOM updates above.
   * Only needed when the model ACTUALLY changed (thinking bar visibility may differ). */
  _scheduleReflow();
}
/* ★ _scheduleReflow: coalesce multiple _reflowToolbar requests into a single
 * rAF callback.  Without this, rapid UI changes (e.g. _resetToolsToDefaults
 * calling _applyModelUI + _applyImageGenUI) would each schedule their own
 * _reflowToolbar, causing 2-3× redundant forced-layout cycles per frame. */
let _reflowPending = false;
function _scheduleReflow() {
  if (_reflowPending) return;
  _reflowPending = true;
  requestAnimationFrame(() => {
    _reflowPending = false;
    _reflowToolbar();
  });
}

/* ★ _reflowToolbar: measure the toolbar's natural (unwrapped) width, then set
 * --toolbar-w on .input-inner so the textarea + toolbar share a cohesive width,
 * so the textarea + toolbar share a cohesive width.
 *
 * How it works:
 *   1. Temporarily set --toolbar-w to 9999px so the toolbar can lay out at its
 *      natural (unwrapped) width without being constrained.
 *   2. Measure all direct children of .input-actions to get the true content width.
 *   3. Set --toolbar-w to that measured value (clamped to viewport - padding).
 *   4. (Removed) --chat-w is no longer synced — chat area is decoupled.
 */
function _reflowToolbar() {
  const inputBox = document.querySelector('.input-box');
  const isIgMode = inputBox && inputBox.classList.contains('ig-active');
  const bar = document.querySelector(isIgMode ? '.ig-toolbar' : '.input-actions');
  if (!bar) return;
  const inputInner = document.querySelector('.input-inner');
  if (!inputInner) return;

  /* 1. Blow out max-width so toolbar lays out naturally */
  inputInner.style.transition = 'none';
  inputInner.style.setProperty('--toolbar-w', '9999px');

  /* 2. Measure children's natural width using getBoundingClientRect for
   *    sub-pixel accuracy (offsetWidth rounds to integer, losing ~0.5px
   *    per child — with 10+ children this can undercount by 5-8px,
   *    causing the model name to be truncated).
   *    Skip .ig-flex spacer — it has flex:1 so at 9999px it expands
   *    to fill all available space, inflating the sum enormously.
   *    Include horizontal margins — they sit outside the border box
   *    but consume space in flex layout on top of gap.
   *    IMPORTANT: Skip margin-left/right = 'auto' — getComputedStyle
   *    resolves auto margins to the USED value (the pixel amount of
   *    distributed free space). At --toolbar-w: 9999px, an auto margin
   *    resolves to thousands of px, grossly inflating the measurement. */
  let w = 0;
  let visibleKids = 0;
  for (const ch of bar.children) {
    if (ch.classList.contains('ig-flex')) continue;
    const cs = getComputedStyle(ch);
    if (cs.display === 'none') continue;
    const rect = ch.getBoundingClientRect();
    if (rect.width === 0) continue;
    /* Only add explicit (non-auto) margins — auto margins distribute
     * free space and are NOT part of the element's intrinsic size.
     * getComputedStyle resolves auto to "auto" (string) in some browsers,
     * or to the used pixel value (huge at 9999px) in others.
     * Guard both: check for "auto" string AND cap at a sane maximum. */
    const mlRaw = cs.marginLeft;
    const mrRaw = cs.marginRight;
    const ml = (mlRaw === 'auto' || parseFloat(mlRaw) > 50) ? 0 : (parseFloat(mlRaw) || 0);
    const mr = (mrRaw === 'auto' || parseFloat(mrRaw) > 50) ? 0 : (parseFloat(mrRaw) || 0);
    w += rect.width + ml + mr;
    visibleKids++;
  }
  const style = getComputedStyle(bar);
  const gap = parseFloat(style.gap) || parseFloat(style.columnGap) || 0;
  const padL = parseFloat(style.paddingLeft) || 0;
  const padR = parseFloat(style.paddingRight) || 0;
  w += gap * Math.max(0, visibleKids - 1) + padL + padR;

  /* 3. Clamp to viewport and apply.
   *    Round up (ceil) so sub-pixel fractions don't cause compression.
   *    Add 1px safety margin to absorb any remaining rounding in
   *    browser's gap/margin computation that getBoundingClientRect
   *    might not perfectly capture. */
  w = Math.ceil(w) + 1;
  const vw = document.documentElement.clientWidth;
  const maxW = vw - 48; /* 24px padding each side */
  w = Math.max(480, Math.min(w, maxW));
  /* Add border width of .input-box (varies by theme: 1.5px default, 2.5px tofu) */
  const boxBorder = inputBox
    ? (parseFloat(getComputedStyle(inputBox).borderLeftWidth) || 0)
      + (parseFloat(getComputedStyle(inputBox).borderRightWidth) || 0)
    : 3;
  w += boxBorder;

  inputInner.style.setProperty('--toolbar-w', w + 'px');

  /* Re-enable transition after a frame so the initial set is instant */
  requestAnimationFrame(() => {
    inputInner.style.transition = '';
  });

}
/* ★ Re-measure toolbar after web fonts finish loading — font-display:swap
 * means the first _reflowToolbar may measure with fallback font metrics.
 * Once the real font loads, glyph widths change and the preset label
 * may need more space. */
if (document.fonts && document.fonts.ready) {
  document.fonts.ready.then(() => _scheduleReflow());
}
/* --chat-w is NOT synced — chat area uses its own fixed max-width (820px default)
 * independent of toolbar width, per §4.2 decoupled layout. */

/* Backward-compat alias so old callers still work during transition */
function _applyPresetUI(presetOrModel) { _applyModelUI(presetOrModel); }

/* ── Thinking Depth Selection ── */
function selectThinkingDepth(depth) {
  config.thinkingDepth = depth;
  /* ★ Sync thinkingEnabled: 'off' disables thinking for thinking-capable models */
  thinkingEnabled = depth !== 'off';
  /* ★ PERF: Lightweight path — only update depth-related UI elements.
   * The previous code called _applyModelUI(config.model) which:
   *   1. Iterated ALL model dropdown items via querySelectorAll
   *   2. Scheduled _reflowToolbar (3-4 forced synchronous layouts)
   * None of that is needed for a depth toggle — the model hasn't changed,
   * the toolbar structure hasn't changed, only the badge text and active
   * button highlight need updating. */
  _updateDepthButtons(depth);
  const shortName = _modelShortName(config.model);
  const modelBadge = document.getElementById("modelBadge");
  if (modelBadge) {
    if (depth === 'off') {
      modelBadge.textContent = shortName;
    } else {
      modelBadge.innerHTML = `${shortName} &middot; ${_DEPTH_ICONS[depth] || _DEPTH_ICON_FALLBACK} ${_DEPTH_LABELS[depth] || depth}`;
    }
  }
  const toggle = document.getElementById("presetToggle");
  if (toggle) {
    const iconEl = toggle.querySelector(".ps-icon");
    if (iconEl) {
      const brand = typeof _detectBrand === 'function' ? _detectBrand(config.model) : 'generic';
      if (brand === 'generic') iconEl.innerHTML = _DEPTH_ICONS[depth] || _DEPTH_ICON_FALLBACK;
    }
  }
  /* ★ FIX: Persist depth to conv object immediately.  Without this,
   * conv.thinkingDepth stays stale (e.g. 'off') while config.thinkingDepth
   * is updated (e.g. 'max').  If an async operation like
   * loadConversationsFromServer triggers _restoreConvToolState before the
   * user sends, it clobbers config.thinkingDepth back to conv.thinkingDepth
   * → backend receives the stale depth → no thinking generated. */
  _saveConvToolState();
  try { localStorage.setItem("claude_client_config", JSON.stringify(config)); }
  catch (e) { debugLog(`[selectThinkingDepth] localStorage save failed: ${e.message}`, 'error'); }
}

function _updateDepthButtons(activeDepth) {
  /* ★ PERF: Cache the depth button NodeList and use a for-loop instead of
   * querySelectorAll + forEach on every call.  During rapid conv switching,
   * this avoids repeated DOM queries + closure allocation. */
  const buttons = _depthButtonsCache || (_depthButtonsCache = document.querySelectorAll('.depth-btn'));
  for (let i = 0, len = buttons.length; i < len; i++) {
    buttons[i].classList.toggle('active', buttons[i].getAttribute('data-depth') === activeDepth);
  }
}
let _depthButtonsCache = null;
function _applySearchModeUI(mode) {
  const modes = ["off", "single", "multi"];
  if (!modes.includes(mode)) mode = "off";
  searchMode = mode;
  const titles = {
    off: "Search",
    single: "Search",
    multi: "Search",
  };
  const labels = { off: "OFF", single: "1×", multi: "∞" };
  const badgeTexts = { single: "1× SEARCH", multi: "∞ MULTI SEARCH" };
  const toggle = document.getElementById("searchModeToggle");
  if (toggle) {
    toggle.setAttribute("data-mode", searchMode);
    toggle.querySelector(".sm-label").textContent = titles[searchMode];
    toggle.querySelector(".sm-mode-pill").textContent = labels[searchMode];
  }
  const badge = document.getElementById("searchBadge");
  if (badge) {
    if (searchMode === "off") badge.classList.remove("visible");
    else {
      badge.setAttribute("data-mode", searchMode);
      badge.innerHTML = `<span class="sb-dot"></span>${badgeTexts[searchMode]}`;
      badge.classList.add("visible");
    }
  }
  // ★ fetch is bundled with search — auto-enable when search is on
  if (mode !== "off") {
    _applyFetchEnabledUI(true);
  }
}
function _applyFetchEnabledUI(enabled) {
  fetchEnabled = true; // always on — no longer toggleable
}
function _applyCodeExecUI(enabled) {
  codeExecEnabled = !!enabled;
  document
    .getElementById("codeExecToggle")
    ?.classList.toggle("active", codeExecEnabled);
  document
    .getElementById("codeExecBadge")
    ?.classList.toggle("visible", codeExecEnabled);
}
function _applyBrowserUI(enabled) {
  browserEnabled = !!enabled;
  document
    .getElementById("browserToggle")
    ?.classList.toggle("active", browserEnabled);
  const badge = document.getElementById("browserBadge");
  if (badge) {
    badge.classList.toggle("visible", browserEnabled);
  }
  _updateBrowserModalBtn();
}
function _applyMemoryUI(enabled) {
  memoryEnabled = !!enabled;
  document
    .getElementById("memoryToggle")
    ?.classList.toggle("active", memoryEnabled);
  document
    .getElementById("memoryBadge")
    ?.classList.toggle("visible", memoryEnabled);
  _updateMemoryModalBtn();
}
function _applySchedulerUI(enabled) {
  schedulerEnabled = !!enabled;
  document
    .getElementById("schedulerToggle")
    ?.classList.toggle("active", schedulerEnabled);
  document
    .getElementById("schedulerBadge")
    ?.classList.toggle("visible", schedulerEnabled);
}
function _applyImageGenToolUI(enabled) {
  imageGenEnabled = !!enabled;
  document
    .getElementById("imageGenToggle")
    ?.classList.toggle("active", imageGenEnabled);
}
function toggleImageGenTool() {
  _applyImageGenToolUI(!imageGenEnabled);
  _saveConvToolState();
  if (typeof updateSubmenuCounts === 'function') updateSubmenuCounts();
  debugLog(`Image Gen Tool: ${imageGenEnabled ? 'ON' : 'OFF'}`, imageGenEnabled ? 'success' : 'info');
}
function _applyHumanGuidanceUI(enabled) {
  humanGuidanceEnabled = !!enabled;
  document
    .getElementById("humanGuidanceToggle")
    ?.classList.toggle("active", humanGuidanceEnabled);
}
function toggleHumanGuidance() {
  _applyHumanGuidanceUI(!humanGuidanceEnabled);
  _saveConvToolState();
  if (typeof updateSubmenuCounts === 'function') updateSubmenuCounts();
  debugLog(`Human Guidance: ${humanGuidanceEnabled ? 'ON' : 'OFF'}`, humanGuidanceEnabled ? 'success' : 'info');
}
let _lastImageGenMode = null;   // track previous state to skip redundant reflows
function _applyImageGenUI(enabled) {
  const prev = imageGenMode;
  imageGenMode = !!enabled;
  const box = document.querySelector('.input-box');
  if (box) box.classList.toggle('ig-active', imageGenMode);
  document.getElementById('imageGenModeBtn')?.classList.toggle('active', imageGenMode);
  // Update placeholder and hint
  const textarea = document.getElementById('userInput');
  if (textarea) textarea.placeholder = imageGenMode
    ? t('ig.placeholder')
    : 'Type your message...';
  const hint = document.getElementById('inputHint');
  if (hint) hint.textContent = imageGenMode
    ? t('ig.hint')
    : 'Enter send · Ctrl+Enter newline · 📎 or drop files';
  /* ★ Reflow toolbar only if the mode actually changed — switching between
   * ig-active / normal swaps the visible toolbar so re-measure is needed.
   * But on conv switch where both convs have imageGenMode=false, skip. */
  if (prev !== imageGenMode || _lastImageGenMode === null) {
    _lastImageGenMode = imageGenMode;
    _scheduleReflow();
  }
}
function toggleImageGen() {
  _applyImageGenUI(!imageGenMode);
  _saveConvToolState();
  if (typeof updateSubmenuCounts === 'function') updateSubmenuCounts();
  debugLog(`Image Gen: ${imageGenMode ? 'ON' : 'OFF'}`, imageGenMode ? 'success' : 'info');
}
function _applyDesktopUI(enabled) {
  desktopEnabled = !!enabled;
  document
    .getElementById("desktopToggle")
    ?.classList.toggle("active", desktopEnabled);
  document
    .getElementById("desktopBadge")
    ?.classList.toggle("visible", desktopEnabled);
}
function toggleDesktop() {
  _applyDesktopUI(!desktopEnabled);
  _saveConvToolState();
}
function _saveConvToolState() {
  const conv = getActiveConv();
  if (!conv) return;
  conv.model = config.model || serverModel;
  conv.thinkingDepth = config.thinkingDepth;
  /* ★ FIX: Track the selected image gen model separately so pure image-gen
   * conversations accurately record which model was actually used, without
   * polluting the chat model field (which _applyModelUI reads on restore). */
  if (imageGenMode) {
    conv.imageGenModel = _igSelectedModel || 'gemini-3.1-flash-image-preview';
    conv.imageGenCount = _igSelectedCount || 1;
    conv.imageGenAspect = _igSelectedAspect || '1:1';
    conv.imageGenResolution = _igSelectedResolution || '1K';
  }
  conv.searchMode = searchMode || "multi";
  conv.fetchEnabled = !!fetchEnabled;
  conv.codeExecEnabled = !!codeExecEnabled;
  conv.browserEnabled = !!browserEnabled;
  conv.desktopEnabled = !!desktopEnabled;
  conv.memoryEnabled = !!memoryEnabled;
  conv.schedulerEnabled = !!schedulerEnabled;
  conv.swarmEnabled = !!swarmEnabled;
  conv.endpointEnabled = !!endpointEnabled;
  conv.imageGenEnabled = !!imageGenEnabled;
  conv.imageGenMode = !!imageGenMode;
  conv.humanGuidanceEnabled = !!humanGuidanceEnabled;
  conv.agentBackend = activeAgentBackend || 'builtin';
  /* ★ FIX: Sync projectPath from the UI-visible projectState to the conv object.
   * Without this, conv.projectPath can diverge from projectState when:
   *  (a) A new conv is created (has no projectPath property at all)
   *  (b) _restoreConvProject succeeds (updates projectState but not conv.projectPath)
   *  (c) The conv was loaded from cache/server without projectPath in settings
   * This divergence causes the bug: "UI shows project B active, but backend gets
   * no project path" because startAssistantResponse reads conv.projectPath (empty)
   * while projectState.path still shows the project. */
  /* Only sync conv.projectPath when projectState is actively showing a project.
   * Do NOT clear conv.projectPath when projectState.active is false, because
   * _restoreConvProject temporarily clears projectState during its async fetch.
   * If we cleared here, a toggle during that gap would destroy the saved path.
   * Explicit clearing is handled by clearProject() → _saveConvProjectPath(""). */
  if (projectState.active && projectState.path) {
    conv.projectPath = projectState.path;
    // Also sync multi-root paths if present
    const allPaths = [projectState.path];
    if (projectState.extraRoots?.length) {
      for (const r of projectState.extraRoots) {
        const p = typeof r === 'string' ? r : r.path;
        if (p && !allPaths.includes(p)) allPaths.push(p);
      }
    }
    conv.projectPaths = allPaths;
  }
  /* ★ FIX: Don't overwrite autoTranslate on a conversation with an active task.
   * The autoTranslate state is frozen at send-time. If the user toggles it OFF
   * while viewing this conv (or switches to another conv with it off), the running
   * task's finishStream() should still use the send-time value, not the current global.
   * This prevents cross-talk: toggling autoTranslate in conv B no longer breaks
   * the pending translation for conv A's running task. */
  const _taskActive = !!(conv.activeTaskId || activeStreams.has(conv.id));
  if (!_taskActive) {
    conv.autoTranslate = !!autoTranslate;
  }
  /* ★ FIX: Pass null instead of conv.id — toggling tools is a metadata-only
   * change, NOT new conversation activity.  Passing conv.id bumps
   * updatedAt = Date.now(), making the conversation jump to the top of the
   * sidebar just because the user toggled a tool button. */
  saveConversations(null);
  /* ★ Lightweight tool-state sync via PATCH (no messages, no msg_count).
   * Only syncs when the conv already exists in the DB (has messages).
   * Uses a debounced fetch to coalesce rapid toggles. */
  if (conv.messages && conv.messages.length > 0) {
    _syncToolStateDebounced(conv);
  } else {
    console.log(`[_saveConvToolState] Skipped server sync — conv ${conv.id.slice(0,8)} has no messages yet`);
  }
}

// ── Debounced lightweight tool-state PATCH ──
const _toolStateDebounceTimers = new Map();
function _syncToolStateDebounced(conv, delayMs = 1500) {
  const existing = _toolStateDebounceTimers.get(conv.id);
  if (existing) clearTimeout(existing);
  _toolStateDebounceTimers.set(conv.id, setTimeout(() => {
    _toolStateDebounceTimers.delete(conv.id);
    const settings = _buildConvSettings(conv);
    fetch(apiUrl(`/api/chat/tool-state/${conv.id}`), {
      method: 'PATCH',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(settings),
    }).then(resp => {
      if (!resp.ok) console.warn(`[ToolState] PATCH failed: HTTP ${resp.status}`);
    }).catch(err => {
      console.warn(`[ToolState] PATCH error: ${err.message}`);
    });
  }, delayMs));
}

function _restoreConvToolState(conv) {
  config.thinkingDepth = conv.thinkingDepth || null;   // ← restore depth BEFORE model UI (let _applyModelUI normalize)
  _applyModelUI(conv.model || conv.preset || conv.effort || serverModel);
  _applySearchModeUI(conv.searchMode || "multi");
  _applyFetchEnabledUI(true);  // always on
  _applyCodeExecUI(!!conv.codeExecEnabled);
  _applyBrowserUI(!!conv.browserEnabled);
  _applyDesktopUI(!!conv.desktopEnabled);
  _applyMemoryUI(conv.memoryEnabled !== undefined ? !!conv.memoryEnabled : true);
  _applySchedulerUI(!!conv.schedulerEnabled);
  _applySwarmUI(!!conv.swarmEnabled);
  _applyEndpointUI(!!conv.endpointEnabled);
  _applyImageGenToolUI(!!conv.imageGenEnabled);
  _applyImageGenUI(!!conv.imageGenMode);
  _applyHumanGuidanceUI(!!conv.humanGuidanceEnabled);
  /* ★ Restore the image gen model + batch count + aspect + resolution from conv settings */
  if (conv.imageGenModel) _igSelectedModel = conv.imageGenModel;
  if (conv.imageGenCount) {
    _igSelectedCount = conv.imageGenCount;
    document.querySelectorAll('#igCountBar .ig-pill').forEach(b =>
      b.classList.toggle('active', parseInt(b.dataset.count) === _igSelectedCount));
    const genText = document.querySelector('.ig-gen-text');
    if (genText) genText.textContent = _igSelectedCount > 1 ? `${_igSelectedCount}连抽!` : '生成';
  }
  /* ★ Restore aspect ratio selection */
  if (conv.imageGenAspect) {
    _igSelectedAspect = conv.imageGenAspect;
    document.querySelectorAll('#igAspectBar .ig-pill').forEach(b =>
      b.classList.toggle('active', b.dataset.ar === _igSelectedAspect));
  }
  /* ★ Restore resolution selection */
  if (conv.imageGenResolution) {
    _igSelectedResolution = conv.imageGenResolution;
    document.querySelectorAll('#igResolutionBar .ig-pill').forEach(b =>
      b.classList.toggle('active', b.dataset.res === _igSelectedResolution));
  }
  _applyAutoTranslateUI(conv.autoTranslate !== undefined ? !!conv.autoTranslate : true);
  /* ★ Restore agent backend selection per-conversation */
  const _savedBackend = conv.agentBackend || 'builtin';
  if (_savedBackend !== activeAgentBackend) {
    activeAgentBackend = _savedBackend;
    // Restore capabilities from cache
    if (_agentBackendCache) {
      const b = _agentBackendCache.find(x => x.name === activeAgentBackend);
      if (b) _agentBackendCapabilities = b.capabilities || {};
    }
    _applyAgentBackendUI();
    _applyBackendCapabilities();
  }
  if (typeof updateSubmenuCounts === 'function') updateSubmenuCounts();
  /* ★ Reflow toolbar after restoring conv tool state (toolbar width may differ). */
  _scheduleReflow();
}
function _resetToolsToDefaults() {
  // ★ Reset agent backend to builtin
  activeAgentBackend = 'builtin';
  _agentBackendCapabilities = null;
  _applyAgentBackendUI();
  _applyBackendCapabilities();
  config.thinkingDepth = config.defaultThinkingDepth;   // ← reset to default depth BEFORE applying model UI (let _applyModelUI normalize)
  _applyModelUI(serverModel);
  _applySearchModeUI("multi");
  _applyFetchEnabledUI(true);
  _applyCodeExecUI(false);
  _applyBrowserUI(false);
  _applyMemoryUI(true);
  _applySwarmUI(false);
  _applyEndpointUI(false);
  _applyImageGenToolUI(false);
  _applyImageGenUI(false);
  if (typeof paperMode !== 'undefined' && paperMode && typeof exitPaperMode === 'function') exitPaperMode();
  _applyAutoTranslateUI(true);
  /* ★ Reset image gen creative mode settings to defaults */
  _igSelectedAspect = '1:1';
  _igSelectedResolution = '1K';
  _igSelectedCount = 1;
  document.querySelectorAll('#igAspectBar .ig-pill').forEach(b =>
    b.classList.toggle('active', b.dataset.ar === '1:1'));
  document.querySelectorAll('#igResolutionBar .ig-pill').forEach(b =>
    b.classList.toggle('active', b.dataset.res === '1K'));
  document.querySelectorAll('#igCountBar .ig-pill').forEach(b =>
    b.classList.toggle('active', parseInt(b.dataset.count) === 1));
  const genText = document.querySelector('.ig-gen-text');
  if (genText) genText.textContent = t('toolbar.generate');
  if (typeof updateSubmenuCounts === 'function') updateSubmenuCounts();
  /* ★ Reflow toolbar after resetting tools (toolbar width may differ). */
  _scheduleReflow();
}
function newChat() {
  _purgeEmptyConvs();
  const prevConv = getActiveConv();
  if (prevConv) {
    prevConv.model = config.model || serverModel;
    prevConv.thinkingDepth = config.thinkingDepth;
    prevConv.searchMode = searchMode || "multi";
    prevConv.fetchEnabled = !!fetchEnabled;
    prevConv.codeExecEnabled = !!codeExecEnabled;
    prevConv.browserEnabled = !!browserEnabled;
    prevConv.memoryEnabled = !!memoryEnabled;
    prevConv.swarmEnabled = !!swarmEnabled;
    prevConv.endpointEnabled = !!endpointEnabled;
    prevConv.imageGenEnabled = !!imageGenEnabled;
    if (imageGenMode) {
      prevConv.imageGenAspect = _igSelectedAspect || '1:1';
      prevConv.imageGenResolution = _igSelectedResolution || '1K';
    }
    prevConv.autoTranslate = !!autoTranslate;
    /* ★ FIX: Save projectPath for the previous conv so it doesn't lose its
     * project association.  This mirrors the same sync logic in _saveConvToolState
     * — without it, prevConv.projectPath could be undefined for convs that were
     * never explicitly set via the project modal (e.g. inherited from projectState). */
    if (projectState.active && projectState.path) {
      prevConv.projectPath = projectState.path;
    }
  }
  const hasInput =
    document.getElementById("userInput").value.trim() ||
    pendingImages.length > 0 ||
    pendingPdfTexts.length > 0 ||
    (_pendingLogClean && _pendingLogClean.originalText);
  activeConvId = null;
  sessionStorage.removeItem('chatui_activeConvId');
  _lastRenderedFingerprint = "";
  /* ★ Show folder context in topbar when creating a new chat from folder view */
  const _newChatFolderId = typeof getActiveFolderId === 'function' ? getActiveFolderId() : null;
  const _newChatFolder = _newChatFolderId && typeof getFolderById === 'function' ? getFolderById(_newChatFolderId) : null;
  const topbarEl = document.getElementById("topbarTitle");
  if (_newChatFolder) {
    topbarEl.innerHTML = `New Chat <span class="topbar-folder-badge" style="color:${_newChatFolder.color || 'var(--text-tertiary)'}">● ${escapeHtml(_newChatFolder.name)}</span>`;
  } else {
    topbarEl.textContent = "New Chat";
  }
  renderConversationList();
  if (typeof clearDebug === "function") clearDebug();
  /* ★ Welcome screen: show folder indicator when new chat will be assigned to a folder */
  const _folderBadgeHtml = _newChatFolder
    ? `<div class="welcome-folder-badge"><span class="welcome-folder-dot" style="color:${_newChatFolder.color || '#888'}">●</span> ${escapeHtml(_newChatFolder.name)}</div>`
    : '';
  document.getElementById("chatInner").innerHTML =
    `<div class="welcome" id="welcome"><div class="welcome-icon"><img src="${BASE_PATH}/static/icons/tofu-welcome.svg" alt="Tofu" width="64" height="64"></div><h2 class="tofu-brand"><span class="tofu-brand-t">T</span><span class="tofu-brand-o1">o</span><span class="tofu-brand-f">f</span><span class="tofu-brand-u">u</span><small>豆腐</small></h2>${_folderBadgeHtml}<p>${t('welcome.subtitle')}</p><div class="feature-pills"><span class="feature-pill">Extended Thinking</span><span class="feature-pill">Search</span><span class="feature-pill">URL Fetch</span><span class="feature-pill">Image Input</span><span class="feature-pill">Co-Pilot</span><span class="feature-pill">Browser</span></div></div>`;
  buildTurnNav(null);
  renderPendingQueueUI(null);
  updateSendButton();
  if (!hasInput) {
    _clearProjectStateLocal();
    _resetToolsToDefaults();
  }
}
function loadConversation(id) {
  _sendGeneration++;           // ★ invalidate any in-flight sendMessage
  _purgeEmptyConvs();
  _editingMsgIdx = null;
  _lastRenderedFingerprint = "";
  // ── Exit branch mode when switching conversations ──
  if (typeof closeBranchPanel === "function" && typeof isBranchModeActive === "function" && isBranchModeActive()) {
    closeBranchPanel();
  }
  /* ★ PERF: Snapshot the outgoing conv's tool state into its in-memory object
   * (cheap property copies), but DEFER the expensive syncConversationToServer
   * (JSON.stringify of all messages + network PUT) to AFTER the new conv renders.
   * Previously this ran synchronously before any rendering, adding 50-500ms+ of
   * JSON serialization time before the user saw any visual change. */
  const prevConv = getActiveConv();
  let _needsDeferredSave = false;
  if (prevConv && prevConv.id !== id) {
    delete prevConv._initialSwitchLoad;   // ★ clear stale flag from previous conv
    prevConv.model = config.model || serverModel;
    prevConv.thinkingDepth = config.thinkingDepth;
    prevConv.searchMode = searchMode || "multi";
    prevConv.fetchEnabled = !!fetchEnabled;
    prevConv.codeExecEnabled = !!codeExecEnabled;
    prevConv.browserEnabled = !!browserEnabled;
    prevConv.desktopEnabled = !!desktopEnabled;
    prevConv.memoryEnabled = !!memoryEnabled;
    prevConv.schedulerEnabled = !!schedulerEnabled;
    prevConv.swarmEnabled = !!swarmEnabled;
    prevConv.endpointEnabled = !!endpointEnabled;
    prevConv.imageGenEnabled = !!imageGenEnabled;
    prevConv.imageGenMode = !!imageGenMode;
    prevConv.humanGuidanceEnabled = !!humanGuidanceEnabled;
    if (imageGenMode) {
      prevConv.imageGenModel = _igSelectedModel || 'gemini-3.1-flash-image-preview';
      prevConv.imageGenCount = _igSelectedCount || 1;
      prevConv.imageGenAspect = _igSelectedAspect || '1:1';
      prevConv.imageGenResolution = _igSelectedResolution || '1K';
    }
    const _prevTaskActive = !!(prevConv.activeTaskId || activeStreams.has(prevConv.id));
    if (!_prevTaskActive) {
      prevConv.autoTranslate = !!autoTranslate;
    }
    if (projectState.active && projectState.path) {
      prevConv.projectPath = projectState.path;
      const allPaths = [projectState.path];
      if (projectState.extraRoots?.length) {
        for (const r of projectState.extraRoots) {
          const p = typeof r === 'string' ? r : r.path;
          if (p && !allPaths.includes(p)) allPaths.push(p);
        }
      }
      prevConv.projectPaths = allPaths;
    }
    _needsDeferredSave = true;
  }
  activeConvId = id;
  sessionStorage.setItem('chatui_activeConvId', id);
  /* ★ If loading a conv that doesn't belong to the active folder view, exit it */
  if (typeof getActiveFolderId === 'function' && getActiveFolderId()) {
    const _loadedConv = conversations.find(c => c.id === id);
    if (_loadedConv && _loadedConv.folderId !== getActiveFolderId()) {
      setActiveFolderId(null);
    }
  }
  if (typeof closeBranchPanel === "function") closeBranchPanel();
  const c = conversations.find((x) => x.id === id);
  if (!c) return;
  document.getElementById("topbarTitle").textContent = c.title;
  /* ★ PERF: Use fast-path O(1) active-class swap instead of O(N) full sidebar rebuild.
   * Full renderConversationList() is only needed when the conv isn't in the DOM yet
   * (e.g. newly created conversation). The fast path just moves the CSS .active class
   * between two existing DOM elements — zero HTML generation, zero innerHTML. */
  if (!_swapActiveConvItem(id)) {
    renderConversationList();
  }

  /* ── On-demand message loading for server-only conversations ── */
  if (c._needsLoad) {
    c._initialSwitchLoad = true;   // ★ flag for renderChat: use full-render, not surgical
    /* ★ FIX: Don't render the loading skeleton immediately — it shows a small
     * centered div at the top of the viewport, and when messages arrive
     * milliseconds later, _forceScrollToBottom jumps to the bottom → visible
     * top→bottom flash.
     *
     * Instead, keep the previous conversation's content visible during the
     * async IndexedDB/server fetch (typically <50ms for cache hits).  When
     * messages arrive, renderChat does a full render + _forceScrollToBottom
     * atomically, so the user sees a direct transition to the new conversation
     * already scrolled to the bottom — no intermediate state.
     *
     * For the rare case where both cache AND server are slow (>400ms), show
     * the skeleton as a fallback so the user knows something is loading. */
    let _skeletonTimer = setTimeout(() => {
      if (activeConvId === id && c._needsLoad) renderChat(c);
    }, 400);
    loadConversationMessages(id).then(() => {
      clearTimeout(_skeletonTimer);
      const stillExists = conversations.find(x => x.id === id);
      if (!stillExists) return;
      if (activeConvId === id) {
        if (activeStreams.has(id)) {
          showStreamingUIForConv(id);
        } else if (c._needsLoad || c.messages.length === 0) {
          renderChat(c);
          if (typeof _restoreConvToolState === "function") _restoreConvToolState(c);
        } else {
          _forceScrollToBottom(null, true);
        }
      }
      delete c._initialSwitchLoad;   // ★ clear flag after initial load completes
      if (!activeStreams.has(id)) _resumePendingTranslations(id);
    });
  } else if (activeStreams.has(id)) {
    showStreamingUIForConv(id);
  } else {
    renderChat(c);
    _resumePendingTranslations(id);
  }

  renderPendingQueueUI(id);
  // ★ Refresh server queue state for this conversation
  _refreshServerQueue(id);
  updateSendButton();
  if (typeof restoreDebugForConv === "function") restoreDebugForConv(id);
  const inp = document.getElementById("userInput"),
    hasInput =
      (inp && inp.value.trim().length > 0) ||
      pendingImages.length > 0 ||
      pendingPdfTexts.length > 0;
  _restoreConvProject(c);
  if (!hasInput) {
    if (!c._needsLoad) _restoreConvToolState(c);
  }

  /* ★ PERF: Deferred save — serialize & sync the outgoing conversation AFTER
   * the new conversation is fully rendered and interactive.  This moves the
   * expensive JSON.stringify + fetch PUT off the critical rendering path.
   * Using setTimeout(0) ensures it runs after the current call stack AND after
   * the browser has painted the new conversation. */
  if (_needsDeferredSave && prevConv) {
    const pc = prevConv;
    setTimeout(() => {
      /* ★ FIX: Pass null instead of pc.id — saving tool state on conversation
       * switch is a metadata-only change, NOT new conversation activity.
       * Passing pc.id would bump updatedAt = Date.now(), which makes the
       * outgoing conversation jump to the top of the sidebar even though
       * the user only viewed it without making any changes. */
      saveConversations(null);
      if (pc.messages && pc.messages.length > 0) {
        syncConversationToServer(pc);
      }
    }, 0);
  }
}
function deleteConversation(id, e) {
  if (e && e.stopPropagation) e.stopPropagation();
  const s = activeStreams.get(id);
  if (s) {
    s.controller.abort();
    activeStreams.delete(id);
  }
  const conv = conversations.find((c) => c.id === id);
  if (conv && conv.activeTaskId)
    fetch(apiUrl(`/api/chat/abort/${conv.activeTaskId}`), {
      method: "POST",
    }).catch(e => debugLog(`[deleteConv] abort failed: ${e.message}`, 'warn'));
  fetch(apiUrl(`/api/conversations/${id}`), { method: "DELETE" }).catch(
    e => debugLog(`[deleteConv] delete failed: ${e.message}`, 'warn'),
  );
  /* ★ Remove from IndexedDB cache */
  ConvCache.remove(id);
  conversations = conversations.filter((c) => c.id !== id);
  _broadcastToTabs("conv_deleted", { convId: id });
  if (activeConvId === id) {
    if (conversations.length > 0) loadConversation(conversations[0].id);
    else newChat();
  } else renderConversationList();
}

// ══════════════════════════════════════════════════════
// ★ Duplicate (copy) a conversation as a completely independent new conversation
// ══════════════════════════════════════════════════════
async function duplicateConversation(id, e) {
  if (e && e.stopPropagation) e.stopPropagation();
  const srcConv = conversations.find((c) => c.id === id);
  if (!srcConv) return;

  // If the source conversation hasn't been loaded from server yet, load it first
  if (srcConv._needsLoad) {
    try {
      await loadConversationMessages(srcConv.id);
    } catch (err) {
      console.warn(`[duplicateConv] Failed to load source conv: ${err.message}`);
      if (typeof showToast === "function") showToast("", "复制失败", "无法加载原始对话内容", 4000);
      return;
    }
  }

  const now = Date.now();
  const newId = generateId();

  // ★ PERF: Show toast immediately for instant feedback
  if (typeof showToast === "function") {
    showToast("", "对话复制中…", `正在复制 "${srcConv.title}"`, 2000);
  }

  // ★ PERF: Defer heavy work (deep clone + serialize) to next frame
  // so the toast renders immediately without blocking
  requestAnimationFrame(() => {
    // Deep-clone messages, stripping runtime/streaming state
    const clonedMessages = JSON.parse(JSON.stringify(srcConv.messages || [])).map(msg => {
      delete msg._taskId;
      delete msg.activeTaskId;
      delete msg.approvalRequired;
      if (msg.branches) {
        for (const b of msg.branches) delete b.activeTaskId;
      }
      return msg;
    });

    const newConv = {
      id: newId,
      title: (srcConv.title || "Untitled") + " (副本)",
      messages: clonedMessages,
      createdAt: now,
      updatedAt: now,
      activeTaskId: null,
      ...(srcConv.projectPath ? { projectPath: srcConv.projectPath } : {}),
      ...(srcConv.projectPaths ? { projectPaths: [...srcConv.projectPaths] } : {}),
      ...(srcConv.model ? { model: srcConv.model } : {}),
      ...(srcConv.thinkingDepth !== undefined ? { thinkingDepth: srcConv.thinkingDepth } : {}),
      ...(srcConv.searchMode ? { searchMode: srcConv.searchMode } : {}),
      ...(srcConv.fetchEnabled !== undefined ? { fetchEnabled: srcConv.fetchEnabled } : {}),
      ...(srcConv.codeExecEnabled !== undefined ? { codeExecEnabled: srcConv.codeExecEnabled } : {}),
      ...(srcConv.browserEnabled !== undefined ? { browserEnabled: srcConv.browserEnabled } : {}),
      ...(srcConv.memoryEnabled !== undefined ? { memoryEnabled: srcConv.memoryEnabled } : {}),
      ...(srcConv.autoTranslate !== undefined ? { autoTranslate: srcConv.autoTranslate } : {}),
      ...(srcConv.folderId ? { folderId: srcConv.folderId } : {}),
    };

    // Add to conversation list (at top) & render sidebar
    conversations.unshift(newConv);
    saveConversations(newConv.id);

    // ★ PERF: Sync to server in background — don't block UI
    syncConversationToServer(newConv).catch(err => {
      console.warn('[duplicateConv] Background sync failed:', err);
    });

    // Switch to the new conversation
    loadConversation(newId);

    if (typeof showToast === "function") {
      showToast("", "对话已复制 ✓", `"${srcConv.title}" → 独立副本已创建`, 3000);
    }
    console.log(`[duplicateConv] Duplicated conv ${id.slice(0,8)} → ${newId.slice(0,8)} (${clonedMessages.length} msgs)`);
  });
}

// ══════════════════════════════════════════════════════

// ══════════════════════════════════════════════════════
//  ★ Shared config/settings builders for /api/chat/send and /api/chat/regenerate
// ══════════════════════════════════════════════════════

/**
 * Build the task config object from per-conversation state.
 * Used by sendMessage, saveEditAndResend, regenerateFromUser for atomic backend calls.
 */
function _buildConvConfig(conv) {
  const _isActive = (conv.id === activeConvId);
  return {
    maxTokens: config.maxTokens,
    thinkingEnabled,
    model: _isActive ? (config.model || serverModel) : (conv.model || serverModel),
    preset: _isActive ? (config.model || serverModel) : (conv.model || serverModel),
    systemPrompt: config.systemPrompt || '',
    thinkingDepth: _isActive ? config.thinkingDepth : (conv.thinkingDepth || null),
    temperature: config.temperature,
    searchMode: _isActive ? searchMode : (conv.searchMode || "multi"),
    fetchEnabled: _isActive ? fetchEnabled : (!!conv.fetchEnabled),
    codeExecEnabled: _isActive ? codeExecEnabled : (!!conv.codeExecEnabled),
    memoryEnabled: _isActive ? memoryEnabled : (conv.memoryEnabled !== undefined ? !!conv.memoryEnabled : true),
    schedulerEnabled: _isActive ? schedulerEnabled : (!!conv.schedulerEnabled),
    swarmEnabled: _isActive ? swarmEnabled : (!!conv.swarmEnabled),
    projectPath: _getConvProjectPath(conv),
    projectPaths: conv.projectPaths || [],
    autoApply: autoApplyWrites,
    browserEnabled: _isActive ? browserEnabled : (!!conv.browserEnabled),
    desktopEnabled: _isActive ? desktopEnabled : (!!conv.desktopEnabled),
    imageGenEnabled: _isActive ? imageGenEnabled : (!!conv.imageGenEnabled),
    humanGuidanceEnabled: _isActive ? humanGuidanceEnabled : (!!conv.humanGuidanceEnabled),
    endpointMode: _isActive ? endpointEnabled : (!!conv.endpointEnabled),
    agentBackend: activeAgentBackend || 'builtin',
    autoTranslate: conv.autoTranslate !== undefined ? !!conv.autoTranslate : !!autoTranslate,
    browserClientId: (_isActive ? browserEnabled : (!!conv.browserEnabled)) ? (window._browserClientId || null) : null,
    keepToolHistory: config.keepToolHistory !== false,
  };
}

/**
 * Build the per-conversation settings dict for server persistence.
 * Mirrors what syncConversationToServer sends in the PUT settings.
 */
function _buildConvSettings(conv) {
  return {
    model: conv.model || config.model || serverModel,
    preset: conv.model || config.model || serverModel,
    thinkingDepth: conv.thinkingDepth || config.thinkingDepth,
    searchMode: conv.searchMode || searchMode,
    fetchEnabled: !!conv.fetchEnabled,
    codeExecEnabled: !!conv.codeExecEnabled,
    browserEnabled: !!conv.browserEnabled,
    desktopEnabled: !!conv.desktopEnabled,
    memoryEnabled: conv.memoryEnabled !== undefined ? !!conv.memoryEnabled : true,
    schedulerEnabled: !!conv.schedulerEnabled,
    swarmEnabled: !!conv.swarmEnabled,
    endpointEnabled: !!conv.endpointEnabled,
    imageGenEnabled: !!conv.imageGenEnabled,
    humanGuidanceEnabled: !!conv.humanGuidanceEnabled,
    projectPath: conv.projectPath || '',
    projectPaths: conv.projectPaths || [],
    autoTranslate: conv.autoTranslate !== undefined ? !!conv.autoTranslate : !!autoTranslate,
    folderId: conv.folderId || null,
  };
}

/**
 * Render a translating indicator bubble in the chat DOM.
 * Shown while the server translates the user's message before starting the agent.
 * Removed when the real streaming bubble appears.
 */
function _renderTranslatingBubble() {
  const inner = document.getElementById("chatInner");
  if (!inner) return;
  // Remove any previous translating bubble
  const old = document.getElementById("translating-msg");
  if (old) old.remove();
  const el = document.createElement("div");
  el.className = "message message-new";
  el.addEventListener('animationend', () => el.classList.remove('message-new'), { once: true });
  el.id = "translating-msg";
  const avatar = (typeof _TOFU_WORKER_SVG !== 'undefined') ? _TOFU_WORKER_SVG : '✦';
  el.innerHTML = `<div class="message-avatar">${avatar}</div><div class="message-content"><div class="message-header"><span class="message-role">Agent</span><span class="message-time">${new Date().toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" })}</span></div><div class="message-body"><div class="stream-status"><div class="pulse"></div> ${t('sidebar.translating')}</div></div></div>`;
  inner.appendChild(el);
  scrollToBottom();
}

function _removeTranslatingBubble() {
  const el = document.getElementById("translating-msg");
  if (el) el.remove();
}

/**
 * Render the streaming assistant bubble in the chat DOM.
 * Shared by sendMessage and regenerateFromUser flows.
 */
function _renderStreamingBubble(conv, sendConfig) {
  const inner = document.getElementById("chatInner");
  if (!inner) return;
  const role = _streamingBubbleRole(conv, sendConfig);
  inner.insertAdjacentHTML('beforeend', _streamingBubbleHTML(role));
  const el = document.getElementById('streaming-msg');
  if (el) {
    el.classList.add('message-new');
    el.addEventListener('animationend', () => el.classList.remove('message-new'), { once: true });
  }
  scrollToBottom();
}

async function startAssistantResponse(convId) {
  const conv = conversations.find((c) => c.id === convId);
  if (!conv || activeStreams.has(convId) || conv.activeTaskId) return;
  /* ★ Ensure messages are loaded — Case E recovery can call this for _needsLoad shell convs
   *   that were detected as orphans via metadata (lastMsgRole/lastMsgTimestamp).
   *   Without loading first, conv.messages is empty → the server-side message builder has no context. */
  if (conv._needsLoad) {
    console.info(`[startAssistantResponse] Loading messages for shell conv ${convId.slice(0,8)} before starting`);
    await loadConversationMessages(convId);
    if (conv.messages.length === 0) {
      console.warn(`[startAssistantResponse] conv ${convId.slice(0,8)} still has 0 messages after load — aborting`);
      return;
    }
  }
  /* ★ Use per-conv model — config.model is global and may reflect a different conv */
  const _convModel = (convId === activeConvId) ? (config.model || serverModel) : (conv.model || serverModel);
  const assistantMsg = {
    role: "assistant",
    content: "",
    thinking: "",
    timestamp: Date.now(),
    toolRounds: [],
    model: _convModel,
  };
  conv.messages.push(assistantMsg);
  if (activeConvId === convId) {
    const inner = document.getElementById("chatInner");
    if (inner) {
      /* ★ Endpoint mode: show Planner avatar/role from the start.
       * The endpoint_iteration(planning) event may fire before SSE connects,
       * so the initial bubble must already show "Planner". */
      const _isEndpoint = (convId === activeConvId) ? endpointEnabled : (!!conv.endpointEnabled);
      if (_isEndpoint) assistantMsg._isEndpointPlanner = true;
      const role = _isEndpoint ? 'planner' : 'worker';
      inner.insertAdjacentHTML('beforeend', _streamingBubbleHTML(role));
      const el = document.getElementById('streaming-msg');
      if (el) {
        el.classList.add('message-new');
        el.addEventListener('animationend', () => el.classList.remove('message-new'), { once: true });
      }
      scrollToBottom();
    }
  }
  /* ★ Don't persist the empty assistant message yet — wait until we have a taskId.
        This prevents a "ghost" empty message if the user refreshes before POST returns. */
  buildTurnNav(conv);
  // ★ Server-side message building: the backend loads messages from DB,
  //   so we MUST sync to server BEFORE the POST to ensure DB is up-to-date.
  //   We sync all messages EXCEPT the trailing empty assistant (it's just a
  //   UI placeholder — the backend doesn't need it).
  await syncConversationToServer(conv);
  // Debug panel shows raw conv.messages for inspection (not API-processed).
  showMessagesInDebug(
    conv.messages.slice(0, -1),  // exclude trailing empty assistant msg
    `${conv.messages.length} ${t('conv.messages')}`,
    false,
    convId,
  );
  let taskId;
  /* ★ Use shared _buildConvConfig to avoid config divergence.
   * startAssistantResponse is now only used by continueAssistant() and
   * Case E orphan recovery. sendMessage and regenerateFromUser use the
   * atomic /api/chat/send and /api/chat/regenerate endpoints instead. */
  const baseConfig = _buildConvConfig(conv);
  const _ep = !!baseConfig.endpointMode;
  const _pp = baseConfig.projectPath;
  /* Decide API route: endpoint mode uses /api/endpoint/start */
  const startUrl = _ep
    ? apiUrl("/api/endpoint/start")
    : apiUrl("/api/chat/start");
  try {
    const resp = await fetch(startUrl, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        convId,
        config: baseConfig,
      }),
      signal: typeof AbortSignal.timeout === 'function'
        ? AbortSignal.timeout(30000)    // 30s timeout to prevent infinite hang
        : undefined,
    });
    if (!resp.ok) {
      const err = await resp
        .json()
        .catch(() => ({ error: `HTTP ${resp.status}` }));
      throw new Error(err.error || `Server ${resp.status}`);
    }
    const data = await resp.json();
    taskId = data.taskId;
    debugLog(
      `Task started: ${taskId} (${_ep ? "ENDPOINT" : "normal"} search:${baseConfig.searchMode} fetch:${baseConfig.fetchEnabled} project:${_pp ? "yes" : "no"} browser:${baseConfig.browserEnabled} autoApply:${baseConfig.autoApply}${convId === activeConvId ? "" : " bg-conv"})`,
      "success",
    );
  } catch (e) {
    const errMsg = e.name === 'TimeoutError'
      ? 'Request timed out — server may be overloaded or unreachable. Try restarting the server.'
      : e.name === 'AbortError' ? 'Request was aborted'
      : e.message;
    debugLog("Failed: " + errMsg, "error");
    console.error('[startAssistantResponse] POST failed:', e.name, e.message);
    assistantMsg.error = errMsg;
    if (activeConvId === convId) {
      const sm = document.getElementById("streaming-msg");
      if (sm)
        sm.outerHTML = renderMessage(assistantMsg, conv.messages.length - 1);
    }
    saveConversations(convId);
    /* ★ Sync to server even on error — this conversation only exists in
     * memory until synced.  Without this, a page refresh loses it. */
    syncConversationToServer(conv);
    buildTurnNav(conv);
    return;
  }
  /* ★ Now we have taskId — persist atomically: activeTaskId + assistantMsg together */
  conv.activeTaskId = taskId;
  /* ★ CROSS-TALK DETECTION: log task→conv binding at creation time */
  console.info(
    `[startAssistantResponse] 🎯 Task bound: task=${taskId.slice(0,8)} → conv=${convId.slice(0,8)} ` +
    `msgs=${conv.messages.length} isActive=${activeConvId === convId} ` +
    `otherActiveStreams=[${[...activeStreams.keys()].filter(k=>k!==convId).map(k=>k.slice(0,8)).join(',')}]`
  );
  saveConversations(convId);
  syncConversationToServer(conv);
  /* ★ Only manipulate DOM if this conv is still active */
  if (activeConvId === convId) {
    const oldSm = document.getElementById("streaming-msg");
    if (oldSm) oldSm.remove();
  }
  connectToTask(convId, taskId);
}

async function sendMessage() {
  // ── Image Gen mode intercept: redirect to direct generation ──
  if (imageGenMode) { generateImageDirect(); return; }
  // ── Branch mode intercept: redirect to branch if active ──
  if (typeof isBranchModeActive === "function" && isBranchModeActive()) {
    const branchCtx = getActiveBranchContext();
    if (branchCtx) {
      const input = document.getElementById("userInput");
      const text = (input?.value || "").trim();
      if (!text && pendingImages.length === 0) return;
      // Collect images and clear, then delegate to branch sender
      const imgs = [...pendingImages];
      pendingImages = [];
      renderImagePreviews();
      input.value = "";
      input.style.height = "auto";
      sendBranchMessage(text, imgs.length ? imgs : null);
      return;
    }
  }
  const input = document.getElementById("userInput");
  const text = input.value.trim();
  if (!text && pendingImages.length === 0 && pendingPdfTexts.length === 0)
    return;
  if (pdfProcessing) return;
  // If log noise banner is showing, ask user first
  if (_pendingLogClean) {
    const r = _pendingLogClean;
    const opsDesc = r.ops.map((o) => o.desc).join("、");
    const doClean = confirm(
      `检测到日志噪音，可节省 ${r.savedChars.toLocaleString()} 字符（${r.savedPct}%）\n\n清理项: ${opsDesc}\n\n点击「确定」清理后发送，「取消」保持原文发送。`,
    );
    if (doClean) {
      input.value = input.value.replace(r.originalText, r.cleanedText);
      debugLog(
        `Log noise auto-cleaned on send: saved ${r.savedChars} chars`,
        "success",
      );
    }
    hideLogCleanBanner();
    if (doClean) {
      // Re-read the cleaned text
      const newText = input.value.trim();
      if (
        !newText &&
        pendingImages.length === 0 &&
        pendingPdfTexts.length === 0
      )
        return;
    }
  }
  const finalText = input.value.trim();
  if (!finalText && pendingImages.length === 0 && pendingPdfTexts.length === 0)
    return;
  const sendGen = ++_sendGeneration;   // ★ capture generation for staleness checks
  let conv = getActiveConv();
  /* Ensure messages are loaded before sending */
  if (conv && conv._needsLoad) await loadConversationMessages(conv.id);
  /* ★ Race guard: user switched conv during the await above */
  if (_sendGeneration !== sendGen) { console.log('[sendMessage] aborted — conv switched during loadMessages'); return; }

  if (!conv) {
    const now = Date.now();
    conv = {
      id: generateId(),
      title: "New Chat",
      messages: [],
      createdAt: now,
      updatedAt: now,
      activeTaskId: null,
    };
    /* ★ FIX: Inherit projectPath from the currently visible projectState.
     * When the user clicks "New Chat" while a project is active and types
     * immediately (hasInput=true), the project UI stays visible but the new
     * conv object had no projectPath → backend received empty string → tools
     * disabled. Now the new conv inherits the displayed project. */
    if (projectState.active && projectState.path) {
      conv.projectPath = projectState.path;
      const allPaths = [projectState.path];
      if (projectState.extraRoots?.length) {
        for (const r of projectState.extraRoots) {
          const p = typeof r === 'string' ? r : r.path;
          if (p && !allPaths.includes(p)) allPaths.push(p);
        }
      }
      conv.projectPaths = allPaths;
    }
    /* ★ Auto-assign to active folder when creating a new conversation in folder view */
    const _curFolderId = typeof getActiveFolderId === 'function' ? getActiveFolderId() : null;
    if (_curFolderId) conv.folderId = _curFolderId;
    conversations.unshift(conv);
    activeConvId = conv.id;
    sessionStorage.setItem('chatui_activeConvId', conv.id);
    _saveConvToolState();
    renderConversationList();
  }
  const convId = conv.id;

  // ── Build message payload for backend ──
  const msgPayload = {
    text: finalText,
    images: [...pendingImages],
    pdfTexts: [...pendingPdfTexts],
    timestamp: Date.now(),
  };
  // Reply quotes
  if (typeof getPendingReplyQuotes === "function") {
    const rqs = getPendingReplyQuotes();
    if (rqs && rqs.length > 0) {
      msgPayload.replyQuotes = rqs;
      clearReplyQuote();
    }
  }
  // Conversation references — send only {id, title}, backend resolves full text
  if (typeof getPendingConvRefs === "function") {
    const crs = getPendingConvRefs();
    if (crs && crs.length > 0) {
      msgPayload.convRefs = crs;
      clearConvRefs();
    }
  }
  // Auto-assign folder
  const _curFolderId = typeof getActiveFolderId === 'function' ? getActiveFolderId() : null;
  if (_curFolderId) msgPayload.folderId = _curFolderId;

  // ── Wait for VLM parsing before sending ──
  const _tempUserMsg = { pdfTexts: msgPayload.pdfTexts };
  await _waitForVlmParsing(_tempUserMsg, convId, -1);
  msgPayload.pdfTexts = _tempUserMsg.pdfTexts;

  // ── Optimistic UI: render user message immediately ──
  const userMsg = {
    role: "user",
    content: finalText,
    images: msgPayload.images,
    pdfTexts: msgPayload.pdfTexts,
    timestamp: msgPayload.timestamp,
  };
  if (msgPayload.replyQuotes) userMsg.replyQuotes = msgPayload.replyQuotes;
  if (msgPayload.convRefs) userMsg.convRefs = msgPayload.convRefs;

  conv._needsLoad = false;
  conv.messages.push(userMsg);
  const userMsgIdx = conv.messages.length - 1;
  if (conv.messages.filter((m) => m.role === "user").length === 1 && finalText) {
    const titleText = stripNoTranslateTags(finalText);
    conv.title = titleText.slice(0, 60) + (titleText.length > 60 ? "..." : "");
    if (activeConvId === convId)
      document.getElementById("topbarTitle").textContent = conv.title;
    renderConversationList();
  }
  input.value = "";
  input.style.height = "auto";
  pendingImages = [];
  pendingPdfTexts = [];
  if (typeof _vlmClearState === 'function') _vlmClearState();
  renderImagePreviews();
  document.getElementById("pdfProgress").style.display = "none";
  if (activeConvId === convId) {
    const w = document.getElementById("welcome");
    if (w) w.remove();
    const chatInnerEl = document.getElementById("chatInner");
    if (chatInnerEl) chatInnerEl.insertAdjacentHTML("beforeend", renderMessage(userMsg, userMsgIdx));
    const newEl = document.getElementById("msg-" + userMsgIdx);
    if (newEl) {
      newEl.classList.add("message-new");
      newEl.addEventListener('animationend', () => newEl.classList.remove('message-new'), { once: true });
    }
    scrollToBottom(true);
  }

  // ── Atomic backend call: message creation + translate + task start ──
  const _sendConfig = _buildConvConfig(conv);

  // ★ If autoTranslate is on and text has Chinese, show stop button immediately
  //   so the user can abort during server-side translation.
  const _sendAbortCtrl = new AbortController();
  const _sendTimeout = setTimeout(() => _sendAbortCtrl.abort(), 60000); // 60s safety timeout
  const _willTranslate = _sendConfig.autoTranslate && /[\u4e00-\u9fff\u3400-\u4dbf]/.test(finalText);
  if (_willTranslate) {
    conv._translating = true;
    conv._translateAborted = false;
    conv._translateAbortCtrl = _sendAbortCtrl;
    updateSendButton();
    renderConversationList();
    if (activeConvId === convId) _renderTranslatingBubble();
  }

  try {
    // ★ If the user just aborted a task, pass its ID so the backend can
    //   abort it immediately — even if the fire-and-forget abort fetch
    //   hasn't arrived yet. This eliminates the race where the send
    //   arrives before the abort and the backend incorrectly enqueues.
    const _sendBody = {
        convId,
        message: msgPayload,
        config: _sendConfig,
        settings: _buildConvSettings(conv),
    };
    if (conv._lastAbortedTaskId) {
      _sendBody.abortTaskId = conv._lastAbortedTaskId;
      delete conv._lastAbortedTaskId;
    }
    const resp = await fetch(apiUrl('/api/chat/send'), {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(_sendBody),
      signal: _sendAbortCtrl.signal,
    });
    if (!resp.ok) {
      const err = await resp.json().catch(() => ({ error: `HTTP ${resp.status}` }));
      throw new Error(err.error || `Server ${resp.status}`);
    }
    const result = await resp.json();

    // Update local state with server response
    if (result.userMessage) {
      // Server may have translated the message — update local copy
      Object.assign(userMsg, result.userMessage);
      if (activeConvId === convId) {
        const msgEl = document.getElementById('msg-' + userMsgIdx);
        if (msgEl) msgEl.outerHTML = renderMessage(userMsg, userMsgIdx);
      }
    }
    if (result.title && result.title !== conv.title) {
      conv.title = result.title;
      if (activeConvId === convId)
        document.getElementById("topbarTitle").textContent = conv.title;
      renderConversationList();
    }
    conv._serverMsgCount = result.msgCount || conv.messages.length;

    // ★ Backend decided to queue (active task running for this conversation)
    if (result.queued) {
      console.log(
        `%c[Queue] ✚ Server queued message %c#${result.position}%c for conv=${convId.slice(0,8)} queueId=${result.queueId?.slice(0,8)}`,
        'color:#a78bfa;font-weight:bold', 'color:#fbbf24;font-weight:bold', 'color:#a78bfa'
      );
      // ★ Remove the optimistic user message from conv.messages and DOM —
      //   queued messages should only appear in the queue bar, not in chatInner.
      //   The backend did NOT persist this message to the conversation DB.
      if (conv.messages[userMsgIdx] === userMsg) {
        conv.messages.splice(userMsgIdx, 1);
        if (activeConvId === convId) {
          const msgEl = document.getElementById('msg-' + userMsgIdx);
          if (msgEl) msgEl.remove();
        }
      }
      // Sync queue state from server for accurate UI
      _refreshServerQueue(convId);
      saveConversations(convId);
      if (activeConvId === convId) {
        _removeTranslatingBubble();
      }
      buildTurnNav(conv);
      renderConversationList();
      updateSendButton();
      debugLog(`消息已排队 (#${result.position})，将在当前回复结束后自动发送`, 'info');
      return;  // ← exit try block, falls through to finally
    }

    // Push empty assistant msg + connect to task SSE
    const taskId = result.taskId;
    const assistantMsg = {
      role: "assistant", content: "", thinking: "",
      timestamp: Date.now(), toolRounds: [],
      model: _sendConfig.model || serverModel,
    };
    // ★ Endpoint mode: mark as planner so SSE reconnection identifies it correctly
    if (_sendConfig.endpointMode) assistantMsg._isEndpointPlanner = true;
    conv.messages.push(assistantMsg);
    conv.activeTaskId = taskId;
    saveConversations(convId);

    debugLog(`Task started: ${taskId} (send)`, "success");

    if (activeConvId === convId) {
      _removeTranslatingBubble();
      _renderStreamingBubble(conv, _sendConfig);
    }
    buildTurnNav(conv);
    connectToTask(convId, taskId);

  } catch (e) {
    // ★ If user aborted during translation, keep the message so the user
    //   can edit the typo — don't remove it.  Also abort any server-side
    //   task that may have been started (the server received the request
    //   before the client-side abort fired).
    if (e.name === 'AbortError' && _willTranslate) {
      console.log('%c[sendMessage] ✗ Aborted during translation by user — keeping message for editing', 'color:#f59e0b;font-weight:bold');
      _removeTranslatingBubble();
      // ★ Keep the user message in conv.messages and DOM — user wants to edit it.
      // Re-render to remove any stale translation overlay.
      if (activeConvId === convId) {
        const msgEl = document.getElementById('msg-' + userMsgIdx);
        if (msgEl) msgEl.outerHTML = renderMessage(userMsg, userMsgIdx);
      }
      saveConversations(convId);
      // ★ Sync to server so the DB matches the frontend state (the server
      //   may have persisted its own version with translation + assistant msg).
      syncConversationToServer(conv);
      buildTurnNav(conv);
      // ★ Abort any server-side task that may have started — the server
      //   received the POST before the client abort fired, so it may have
      //   already translated + started a task.  We abort by convId because
      //   we never received the taskId from the response.
      fetch(apiUrl(`/api/chat/abort-conv/${convId}`), { method: 'POST' }).catch(() => {});
    } else {
      const errMsg = e.name === 'TimeoutError'
        ? 'Request timed out — server may be overloaded.'
        : e.message;
      debugLog("Failed: " + errMsg, "error");
      console.error('[sendMessage] /api/chat/send failed:', e.name, e.message);
      // Show error on the user message
      const errAssistant = {
        role: "assistant", content: "", thinking: "",
        error: errMsg, timestamp: Date.now(), toolRounds: [],
      };
      conv.messages.push(errAssistant);
      if (activeConvId === convId) {
        const chatInnerEl = document.getElementById("chatInner");
        if (chatInnerEl)
          chatInnerEl.insertAdjacentHTML("beforeend", renderMessage(errAssistant, conv.messages.length - 1));
      }
      saveConversations(convId);
      syncConversationToServer(conv);
      buildTurnNav(conv);
    }
  } finally {
    clearTimeout(_sendTimeout);
    // ★ Always clean up translating state
    if (_willTranslate) {
      _removeTranslatingBubble();
      conv._translating = false;
      conv._translateAborted = false;
      conv._translateAbortCtrl = null;
      updateSendButton();
      renderConversationList();
    }
  }
}

// ══════════════════════════════════════════════════════
//  ★ Non-blocking background translation → auto-start assistant
// ══════════════════════════════════════════════════════

// ★ REMOVED: _translateThenRespond() — dead code.
// Translation is now handled atomically by the backend in /api/chat/send,
// /api/chat/regenerate, and dispatch_next_queued() in lib/message_queue.py.

// ══════════════════════════════════════════════════════
//  ★ Pending Message Queue — dispatch, UI, cancel
// ══════════════════════════════════════════════════════

// ★ REMOVED: _dispatchQueuedMessage() — dead code.
// Queue dispatch is now handled server-side by dispatch_next_queued() in
// lib/message_queue.py. The frontend polls for auto-dispatched tasks via
// _checkForQueuedTask() (called from finishStream in ui.js).

// ══════════════════════════════════════════════════════
//  ★ VLM Mandatory Wait — block until all PDF VLM parses finish
// ══════════════════════════════════════════════════════

/**
 * Wait for all VLM-parsing PDFs in a user message to complete.
 * Shows a waiting indicator in the chat area while blocking.
 * This ensures the LLM always sees VLM-quality text, never rule-based.
 */
async function _waitForVlmParsing(userMsg, convId, userMsgIdx) {
  if (!userMsg.pdfTexts || userMsg.pdfTexts.length === 0) return;
  // Check if any PDFs are still VLM-parsing
  const parsing = userMsg.pdfTexts.filter(p => p.vlmStatus === 'parsing');
  if (parsing.length === 0) {
    console.log('%c[VLM-Wait] All PDFs already done, no wait needed', 'color:#22c55e');
    return;
  }
  console.log(`%c[VLM-Wait] Waiting for ${parsing.length} PDF(s) to finish VLM parsing…`, 'color:#f59e0b;font-weight:bold');
  // Show waiting indicator
  let _vlmIndicator = null;
  if (activeConvId === convId) {
    _vlmIndicator = document.createElement('div');
    _vlmIndicator.id = 'vlm-wait-indicator';
    _vlmIndicator.className = 'message';
    _vlmIndicator.innerHTML = '<div class="message-avatar"></div><div class="message-content"><div class="message-body"><div class="stream-status"><div class="pulse"></div> Waiting for VLM PDF parsing…</div></div></div>';
    document.getElementById('chatInner')?.appendChild(_vlmIndicator);
    scrollToBottom();
  }
  // Poll until all PDFs finish VLM (done/done-skipped/failed/timeout/unavailable)
  const MAX_VLM_WAIT = 180; // 180 × 1s = 3 minutes max
  for (let attempt = 0; attempt < MAX_VLM_WAIT; attempt++) {
    await new Promise(r => setTimeout(r, 1000));
    const stillParsing = userMsg.pdfTexts.filter(p => p.vlmStatus === 'parsing');
    if (stillParsing.length === 0) {
      console.log(`%c[VLM-Wait] ✓ All PDFs VLM-done (waited ${attempt}s)`, 'color:#22c55e;font-weight:bold');
      break;
    }
    // Update indicator with progress (lightweight — only touches the indicator div)
    if (_vlmIndicator && activeConvId === convId) {
      const progParts = stillParsing.map(p => `${p.name.slice(0,15)}${p.vlmProgress ? ': ' + p.vlmProgress : ''}`);
      _vlmIndicator.querySelector('.stream-status').innerHTML =
        `<div class="pulse"></div> VLM parsing: ${progParts.join(', ')} (${attempt}s)`;
    }
    // NOTE: Do NOT re-render the user message (outerHTML) during the loop —
    // it destroys and recreates the DOM node, causing the browser to clamp
    // scrollTop when the old node is removed, which jumps scroll to the top.
    // The indicator already shows real-time progress; badge update happens once at the end.
  }
  // Final re-render to show completed VLM badges — preserve scroll position
  if (activeConvId === convId) {
    const chatC = document.getElementById('chatContainer');
    const savedTop = chatC ? chatC.scrollTop : 0;
    const msgEl = document.getElementById('msg-' + userMsgIdx);
    if (msgEl) msgEl.outerHTML = renderMessage(userMsg, userMsgIdx);
    if (chatC) chatC.scrollTop = savedTop;
  }
  // Remove indicator
  if (_vlmIndicator && _vlmIndicator.parentNode) {
    _vlmIndicator.remove();
  }
}

/**
 * Render the pending queue indicator above the input area.
 */
function renderPendingQueueUI(convId) {
  let container = document.getElementById("pendingQueueBar");
  const queue = pendingMessageQueue.get(convId);
  if (!queue || queue.length === 0) {
    if (container) {
      container.classList.add('queue-removing');
      setTimeout(() => { if (container && container.parentNode) container.remove(); }, 200);
    }
    return;
  }
  if (!container) {
    container = document.createElement("div");
    container.id = "pendingQueueBar";
    container.className = "pending-queue-bar";
    const queueHost = document.getElementById("pendingQueueContainer");
    if (queueHost) queueHost.appendChild(container);
  }
  container.classList.remove('queue-removing');
  const items = queue.map((item, i) => {
    const preview = item.text
      ? (item.text.length > 60 ? item.text.slice(0, 60) + "…" : item.text)
      : (item.images?.length ? `${item.images.length} 张图片` : "附件");
    // Attachment badges
    const badges = [];
    if (item.images?.length) badges.push(`<span>${item.images.length} img</span>`);
    if (item.pdfTexts?.length) badges.push(`<span>${item.pdfTexts.length} pdf</span>`);
    if (item.convRefs?.length) badges.push(`<span>${item.convRefs.length} ref</span>`);
    if (item.replyQuotes?.length) badges.push(`<span>↩ ${item.replyQuotes.length}</span>`);
    return `<div class="pending-queue-item">
      <span class="queue-item-number">${i + 1}</span>
      <span class="queue-item-text">${escapeHtml(preview)}</span>
      ${badges.length ? `<span class="queue-item-attachments">${badges.join('')}</span>` : ''}
      <button class="queue-item-cancel" onclick="removePendingQueueItem('${convId}', ${i})" title="取消此消息">✕</button>
      ${item.queueId ? '<span class="queue-item-synced" title="已同步到服务器">☁</span>' : ''}
    </div>`;
  }).join("");
  const headerSvg = `<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M12 2v4m0 12v4M4.93 4.93l2.83 2.83m8.48 8.48l2.83 2.83M2 12h4m12 0h4M4.93 19.07l2.83-2.83m8.48-8.48l2.83-2.83"/></svg>`;
  container.innerHTML = `<div class="queue-header">
    ${headerSvg}
    <span>${queue.length} 条消息排队中</span>
    ${queue.length > 1 ? `<button class="queue-clear-all" onclick="clearPendingQueue('${convId}')">全部清空</button>` : ''}
  </div>${items}`;
}

/**
 * Remove a single item from the pending queue (server-backed).
 */
function removePendingQueueItem(convId, idx) {
  const queue = pendingMessageQueue.get(convId);
  if (!queue) return;
  const removed = queue[idx];
  const removedPreview = removed?.text ? removed.text.slice(0, 40) : '(attachment)';
  const queueId = removed?.queueId;

  // Optimistic local removal
  queue.splice(idx, 1);
  console.log(
    `%c[Queue] ✕ Removed%c item #${idx + 1} from conv=${convId.slice(0,8)} | "${removedPreview}" | remaining=${queue.length}`,
    'color:#f59e0b;font-weight:bold', 'color:#f59e0b'
  );
  if (queue.length === 0) pendingMessageQueue.delete(convId);
  renderPendingQueueUI(convId);
  updateSendButton();
  debugLog(`已取消排队消息 #${idx + 1}`, 'info');

  // Server removal
  if (queueId) {
    fetch(apiUrl(`/api/chat/queue/${convId}/${queueId}`), { method: 'DELETE' })
      .then(resp => {
        if (!resp.ok) console.error(`[Queue] Server remove failed: HTTP ${resp.status}`);
      })
      .catch(err => console.error('[Queue] Server remove error:', err));
  }
}

/**
 * Clear all pending queued messages for a conversation (server-backed).
 */
function clearPendingQueue(convId) {
  const count = pendingMessageQueue.get(convId)?.length || 0;
  console.log(
    `%c[Queue] ✕✕ Cleared ALL%c ${count} queued messages for conv=${convId.slice(0,8)}`,
    'color:#ef4444;font-weight:bold', 'color:#ef4444'
  );
  pendingMessageQueue.delete(convId);
  renderPendingQueueUI(convId);
  updateSendButton();
  debugLog(`已清空全部 ${count} 条排队消息`, 'info');

  // Server clear
  fetch(apiUrl(`/api/chat/queue/${convId}`), { method: 'DELETE' })
    .then(resp => {
      if (!resp.ok) console.error(`[Queue] Server clear failed: HTTP ${resp.status}`);
    })
    .catch(err => console.error('[Queue] Server clear error:', err));
}

/**
 * Refresh the local queue state from the server.
 * Called after stream ends and after page load for active conversations.
 */
/**
 * Check if the server auto-dispatched a queued message and created a new task.
 * If found, load the new user message and connect to the task's SSE stream.
 */
async function _checkForQueuedTask(convId, _retryCount = 0) {
  try {
    // ★ Skip if the conversation already has an active task (user may have
    //   already started a new send/regenerate since finishStream scheduled us)
    const _conv = conversations.find(c => c.id === convId);
    if (_conv && (_conv.activeTaskId || activeStreams.has(convId))) {
      console.log(`%c[Queue] Skipping _checkForQueuedTask — conv=${convId.slice(0,8)} already has active task/stream`, 'color:#a78bfa');
      _refreshServerQueue(convId);
      return;
    }
    const resp = await fetch(apiUrl('/api/chat/active'));
    if (!resp.ok) return;
    const activeTasks = await resp.json();
    // ★ Only connect to non-aborted running tasks — an aborted task
    //   is winding down and should not be reconnected to.
    const newTask = activeTasks.find(t => t.convId === convId && t.status === 'running' && !t.aborted);
    if (!newTask) {
      // Server may still be dispatching (persist → dispatch timing gap).
      // Only retry if there are actually queued items — avoids noisy polling
      // after a simple stop (no queue).
      const _hasQueueItems = pendingMessageQueue.has(convId) && pendingMessageQueue.get(convId).length > 0;
      if (_hasQueueItems && _retryCount < 3) {
        const _delay = [800, 1500, 3000][_retryCount];
        console.log(`%c[Queue] No dispatched task yet for conv=${convId.slice(0,8)}, retry #${_retryCount + 1} in ${_delay}ms`, 'color:#a78bfa');
        setTimeout(() => _checkForQueuedTask(convId, _retryCount + 1), _delay);
        return;
      }
      // No new task after retries (or no queue) — refresh queue UI
      _refreshServerQueue(convId);
      return;
    }
    console.log(
      `%c[Queue] ▶ Found auto-dispatched task ${newTask.id.slice(0,8)} for conv=${convId.slice(0,8)}`,
      'color:#34d399;font-weight:bold'
    );

    const conv = conversations.find(c => c.id === convId);
    if (!conv) return;

    // Reload messages from server to pick up the queued user message
    await loadConversationMessages(convId);

    // Create the empty assistant message placeholder
    const assistantMsg = {
      role: 'assistant',
      content: '',
      thinking: '',
      timestamp: Date.now(),
      toolRounds: [],
    };
    conv.messages.push(assistantMsg);
    conv.activeTaskId = newTask.id;
    conv._needsLoad = false;

    if (activeConvId === convId) {
      renderChat(conv);
    }
    renderConversationList();

    // Connect to the new task's SSE stream
    connectToTask(convId, newTask.id);
    debugLog(`服务器已自动发送排队消息`, 'info');

    // Refresh queue UI
    _refreshServerQueue(convId);
  } catch (err) {
    console.warn('[Queue] _checkForQueuedTask error:', err);
  }
}

async function _refreshServerQueue(convId) {
  try {
    const resp = await fetch(apiUrl(`/api/chat/queue/${convId}`));
    if (!resp.ok) {
      console.warn(`[Queue] Server queue fetch failed: HTTP ${resp.status}`);
      return;
    }
    const serverQueue = await resp.json();
    if (serverQueue.length > 0) {
      // Convert server format to local format
      const localQueue = serverQueue.map(item => ({
        queueId: item.queueId,
        text: item.text || '',
        images: item.hasImages ? ['(server)'] : [],
        pdfTexts: item.hasPdfs ? ['(server)'] : [],
        convRefs: item.hasRefs ? ['(server)'] : [],
        replyQuotes: item.hasQuotes ? ['(server)'] : [],
        timestamp: item.timestamp,
      }));
      pendingMessageQueue.set(convId, localQueue);
    } else {
      pendingMessageQueue.delete(convId);
    }
    renderPendingQueueUI(convId);
    updateSendButton();
    console.log(`%c[Queue] Refreshed from server: ${serverQueue.length} items for conv=${convId.slice(0,8)}`, 'color:#6b7280');
  } catch (err) {
    console.warn('[Queue] Failed to refresh from server:', err);
  }
}

// ══════════════════════════════════════════════════════
//  ★ Regenerate from a specific user message
// ══════════════════════════════════════════════════════
async function regenerateFromUser(idx) {
  const conv = getActiveConv();
  if (!conv) return;
  // ★ SyncFix: don't silently early-return on in-flight stream/task — that
  //   leaves the user thinking nothing happened after clicking Stop. Instead,
  //   synchronously hard-cancel the racing task so we can proceed to truncate
  //   and start a new one in the same click.
  if (activeStreams.has(conv.id) || conv.activeTaskId) {
    console.info(`[SyncFix] regenerateFromUser hard-cancelling racing stream — conv=${conv.id.slice(0,8)} activeTaskId=${conv.activeTaskId?.slice(0,8)||'null'}`);
    if (typeof _hardCancelActiveStream === 'function') {
      _hardCancelActiveStream(conv);
    }
  }
  const msg = conv.messages[idx];
  if (!msg || msg.role !== "user") return;

  // ── Image Gen mode intercept: re-generate via direct image API ──
  const _isIgConv = imageGenMode || conv.imageGenMode;
  const _isIgMsg  = msg._isImageGen || (msg.content && msg.content.startsWith('🎨 '));
  if (_isIgConv || _isIgMsg) {
    conv.messages = conv.messages.slice(0, idx);
    saveConversations(conv.id);
    renderChat(conv);
    renderConversationList();
    let prompt = msg.content || '';
    if (prompt.startsWith('🎨 ')) prompt = prompt.slice(2).trim();
    const textarea = document.getElementById('userInput');
    if (textarea) { textarea.value = prompt; }
    if (!imageGenMode) _applyImageGenUI(true);
    generateImageDirect();
    return;
  }

  const convId = conv.id;

  // ── Optimistic UI: truncate local messages ──
  conv.messages = conv.messages.slice(0, idx + 1);
  conv._needsLoad = false;
  conv._serverMsgCount = conv.messages.length;

  /* ── Surgical DOM truncation ── */
  const _syncMsgsBefore = conv.messages.length;
  if (!_surgicalTruncateDOM(conv, idx)) renderChat(conv);
  renderConversationList();
  // ★ SyncFix: invariant + log (matches saveEditAndResend)
  console.assert(
    !document.getElementById("streaming-msg"),
    `[SyncFix] Stale streaming-msg after regenerateFromUser truncate — conv=${convId.slice(0,8)}`
  );
  console.info(`[SyncFix] regenerateFromUser conv=${convId.slice(0,8)} idx=${idx} msgsBefore=${_syncMsgsBefore} msgsAfter=${conv.messages.length} hasStreamingMsg=${!!document.getElementById('streaming-msg')} activeTaskId=${conv.activeTaskId?.slice(0,8)||'null'}`);

  // ── Atomic backend call: truncate + translate + task start ──
  const _regenConfig = _buildConvConfig(conv);

  // ★ If autoTranslate is on and message has Chinese, show stop button immediately
  const _regenAbortCtrl = new AbortController();
  const _regenTimeout = setTimeout(() => _regenAbortCtrl.abort(), 60000); // 60s safety timeout
  const _regenText = msg.content || '';
  const _regenWillTranslate = _regenConfig.autoTranslate && /[\u4e00-\u9fff\u3400-\u4dbf]/.test(_regenText);
  if (_regenWillTranslate) {
    conv._translating = true;
    conv._translateAborted = false;
    conv._translateAbortCtrl = _regenAbortCtrl;
    updateSendButton();
    renderConversationList();
    if (activeConvId === convId) _renderTranslatingBubble();
  }

  try {
    const resp = await fetch(apiUrl('/api/chat/regenerate'), {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        convId,
        truncateToIndex: idx,
        config: _regenConfig,
        settings: _buildConvSettings(conv),
      }),
      signal: _regenAbortCtrl.signal,
    });
    if (!resp.ok) {
      const err = await resp.json().catch(() => ({ error: `HTTP ${resp.status}` }));
      throw new Error(err.error || `Server ${resp.status}`);
    }
    const result = await resp.json();

    // Update local state if server translated the message
    if (result.userMessage) {
      Object.assign(msg, result.userMessage);
      if (activeConvId === convId) {
        const msgEl = document.getElementById('msg-' + idx);
        if (msgEl) msgEl.outerHTML = renderMessage(msg, idx);
      }
    }
    if (result.title) conv.title = result.title;
    conv._serverMsgCount = result.msgCount || conv.messages.length;

    // Push assistant msg + connect to task
    const taskId = result.taskId;
    const assistantMsg = {
      role: "assistant", content: "", thinking: "",
      timestamp: Date.now(), toolRounds: [],
      model: _regenConfig.model || serverModel,
    };
    // ★ Endpoint mode: mark as planner so SSE reconnection identifies it correctly
    if (_regenConfig.endpointMode) assistantMsg._isEndpointPlanner = true;
    conv.messages.push(assistantMsg);
    conv.activeTaskId = taskId;
    saveConversations(convId);

    _removeTranslatingBubble();
    if (activeConvId === convId) _renderStreamingBubble(conv, _regenConfig);
    buildTurnNav(conv);
    connectToTask(convId, taskId);

  } catch (e) {
    if (e.name === 'AbortError' && _regenWillTranslate) {
      console.log('%c[regenerateFromUser] ✗ Aborted during translation by user', 'color:#f59e0b;font-weight:bold');
      _removeTranslatingBubble();
      saveConversations(convId);
      // ★ FIX: sync truncated messages to server (we already sliced conv.messages
      //   above) and abort any server-side task that may have started.
      syncConversationToServer(conv, { allowTruncate: true });
      buildTurnNav(conv);
      fetch(apiUrl(`/api/chat/abort-conv/${convId}`), { method: 'POST' }).catch(() => {});
    } else {
      debugLog("Regenerate failed: " + e.message, "error");
      console.error('[regenerateFromUser] /api/chat/regenerate failed:', e);
      saveConversations(convId);
      syncConversationToServer(conv, { allowTruncate: true });
      buildTurnNav(conv);
    }
  } finally {
    clearTimeout(_regenTimeout);
    if (_regenWillTranslate) {
      _removeTranslatingBubble();
      conv._translating = false;
      conv._translateAborted = false;
      conv._translateAbortCtrl = null;
      updateSendButton();
      renderConversationList();
    }
  }
}

// ══════════════════════════════════════════════════════
//  ★ Continue: resume an interrupted assistant response
//
//  Checkpoint-based continuation:
//  1. Find the latest recoverable checkpoint:
//     - If there are complete tool rounds → checkpoint = end of last
//       complete tool batch.  Discard partial content/thinking after that
//       point and let the LLM regenerate from the tool results.
//     - If no tool rounds → no recoverable checkpoint → full regeneration.
//  2. Roll back toolRounds, content, and thinking to the checkpoint.
//     The user sees only the preserved tool rounds; any discarded partial
//     text is removed from the message before the request is sent.
//  3. Backend receives the same message structure as a normal request
//     with toolHistory injected, and generates a FRESH response.
//  4. No prefix concatenation — the new LLM output IS the message.
// ══════════════════════════════════════════════════════
async function continueAssistant() {
  const conv = getActiveConv();
  if (!conv || activeStreams.has(conv.id) || conv.activeTaskId) return;
  const assistantMsg = conv.messages[conv.messages.length - 1];
  if (!assistantMsg || assistantMsg.role !== "assistant") return;
  if (!assistantMsg.content && !assistantMsg.thinking) {
    // Nothing to continue — message is empty, just regenerate
    conv.messages.pop();
    /* ★ FIX: clear _needsLoad and _serverMsgCount after pop — same reason as regenerateFromUser */
    conv._needsLoad = false;
    conv._serverMsgCount = conv.messages.length;
    await syncConversationToServer(conv, { allowTruncate: true });
    await startAssistantResponse(conv.id);
    return;
  }

  // ═══════════════════════════════════════════════════════════
  // ★ Step 1: Find the latest recoverable checkpoint
  //   Scan toolRounds to find complete tool batches.
  //   A "complete" round has toolCallId, status==="done", and toolContent.
  // ═══════════════════════════════════════════════════════════
  const allRounds = getToolRoundsFromMsg(assistantMsg);
  let toolHistory = [];
  let lastCompleteIdx = -1;  // index in allRounds of last complete entry

  if (allRounds.length > 0) {
    const hasToolCallIds = allRounds.some((r) => r.toolCallId);
    if (hasToolCallIds) {
      const hasLlmRound = allRounds.some((r) => r.llmRound != null);
      const batches = new Map(); // batchKey → [entries]
      let batchKey = 0;
      // Track which batch each round belongs to for rollback
      const roundBatchMap = []; // index → batchKey

      for (let i = 0; i < allRounds.length; i++) {
        const r = allRounds[i];
        if (!r.toolCallId) { roundBatchMap.push(-1); continue; }

        // Is this round complete?
        // ★ FIX: After page refresh, toolContent may be lost from DB
        //   (race: frontend sync overwrote backend's richer checkpoint).
        //   If status==="done" and we have results metadata, treat as
        //   recoverable — reconstruct toolContent from results for toolHistory.
        if (r.status !== "done") {
          debugLog(
            `Tool round #${r.roundNum} (${r.toolName}) not done (status=${r.status}) — checkpoint before it`,
            "warn",
          );
          break;
        }
        if (r.toolContent == null) {
          // Try to reconstruct from results metadata (available after DB round-trip)
          if (r.results && r.results.length > 0) {
            const reconstructed = r.results.map(res =>
              res.snippet || res.title || res.content || ''
            ).filter(Boolean).join('\n') || '[tool result not available]';
            r.toolContent = reconstructed;
            debugLog(
              `Tool round #${r.roundNum} (${r.toolName}) missing toolContent — reconstructed ${reconstructed.length} chars from results`,
              "warn",
            );
          } else {
            debugLog(
              `Tool round #${r.roundNum} (${r.toolName}) missing toolContent and no results — checkpoint before it`,
              "warn",
            );
            break;
          }
        }

        // Determine batch key
        if (hasLlmRound) {
          batchKey = r.llmRound;
        } else {
          const prev = i > 0 ? allRounds[i - 1] : null;
          if (prev && prev.toolCallId && r.roundNum > prev.roundNum + 1) {
            batchKey++;
          }
        }

        if (!batches.has(batchKey)) batches.set(batchKey, []);
        batches.get(batchKey).push(r);
        roundBatchMap.push(batchKey);
        lastCompleteIdx = i;
      }

      // Convert complete batches to toolHistory
      for (const [, batch] of batches) {
        toolHistory.push(_buildToolHistoryRound(batch));
      }
    }
  }

  // ═══════════════════════════════════════════════════════════
  // ★ Step 2: If no checkpoint, fall back to full regeneration
  // ═══════════════════════════════════════════════════════════
  if (toolHistory.length === 0) {
    // No recoverable tool checkpoint — pop the incomplete assistant message
    // and regenerate from scratch (same as clicking "Regenerate").
    debugLog(
      "Continue: no tool checkpoint found — falling back to full regeneration",
      "info",
    );
    showToast(
      "无法续接（无工具调用检查点），将重新生成回复",
      "info",
    );
    conv.messages.pop();
    /* ★ FIX: clear _needsLoad and _serverMsgCount after pop — same reason as regenerateFromUser */
    conv._needsLoad = false;
    conv._serverMsgCount = conv.messages.length;
    if (activeConvId === conv.id) renderChat(conv, false);
    await syncConversationToServer(conv, { allowTruncate: true });
    await startAssistantResponse(conv.id);
    return;
  }

  // ═══════════════════════════════════════════════════════════
  // ★ Step 3: Roll back to checkpoint
  //   - Keep only complete tool rounds in toolRounds
  //   - Discard partial content and thinking (the LLM will regenerate)
  //   - The user sees the tool call history preserved, text regenerated
  // ═══════════════════════════════════════════════════════════
  const keptRounds = allRounds.slice(0, lastCompleteIdx + 1);
  const discardedRounds = allRounds.length - keptRounds.length;

  // ★ FIX: Reconstruct content prefix from completed rounds' assistantContent
  //   instead of wiping everything to "".  Each kept round's assistantContent
  //   is the text the LLM wrote alongside that tool call batch — preserving it
  //   means the user doesn't lose visible output from successful prior rounds.
  let preservedContent = keptRounds
    .map(r => r.assistantContent || "")
    .filter(c => c)
    .join("\n\n");
  const originalContent = assistantMsg.content || "";
  // ★ FIX: After page refresh, assistantContent may be missing from rounds
  //   (backend checkpoint race).  If preservedContent is empty but we have
  //   keptRounds, use the original content up to a reasonable boundary
  //   as the prefix — the LLM only needs to regenerate from the checkpoint.
  if (!preservedContent && keptRounds.length > 0 && originalContent) {
    // Use the full original content as prefix — the backend will inject
    // it via contentPrefix and the LLM will continue from there.
    preservedContent = originalContent;
    debugLog(
      `Continue: assistantContent missing from rounds, using full original content (${originalContent.length} chars) as prefix`,
      "warn",
    );
  }
  const discardedContent = Math.max(0, originalContent.length - preservedContent.length);
  // ★ Preserved thinking = the per-round thinking fields on kept rounds
  //   (tool_dispatch stores them at capture time).  Anything on
  //   assistantMsg.thinking ABOVE that sum belongs to the interrupted
  //   tail and must be discarded (no signature = Claude would reject it).
  const preservedThinkingChars = keptRounds.reduce(
    (n, r) => n + ((r.thinking || "").length),
    0,
  );
  const discardedThinking = Math.max(
    0,
    (assistantMsg.thinking || "").length - preservedThinkingChars,
  );

  assistantMsg.toolRounds = keptRounds;
  assistantMsg.content = preservedContent;
  // NB: assistantMsg.thinking is cleared here — any thinking we want to
  // replay is already stored per-round on keptRounds[i].thinking and will
  // be sent forward via cfgPayload.toolHistory[].thinking.
  assistantMsg.thinking = "";
  // ★ Save the prefix so state/delta handlers can merge correctly
  if (preservedContent) {
    assistantMsg._continueContentPrefix = preservedContent;
  }
  // Clear stale metadata that will be refreshed by the new generation
  delete assistantMsg.finishReason;
  delete assistantMsg.toolSummary;
  delete assistantMsg.error;

  debugLog(
    `Continue checkpoint: keeping ${keptRounds.length} tool entries ` +
    `(preserved ${preservedContent.length} chars content + ` +
    `${preservedThinkingChars} chars thinking from completed rounds), ` +
    `discarded ${discardedRounds} incomplete rounds + ` +
    `${discardedContent} chars new content + ${discardedThinking} chars thinking`,
    "info",
  );
  if (discardedRounds > 0 || discardedContent > 0 || discardedThinking > 0) {
    const preserveParts = [];
    if (preservedContent.length > 0) preserveParts.push(`${preservedContent.length} 字符内容`);
    if (preservedThinkingChars > 0) preserveParts.push(`${preservedThinkingChars} 字符思考内容`);
    const preserveNote = preserveParts.length > 0
      ? ` (保留了 ${preserveParts.join(" + ")})`
      : '';
    const discardParts = [];
    if (discardedContent > 0) discardParts.push(`${discardedContent} 字符后续文本`);
    if (discardedThinking > 0) discardParts.push(`${discardedThinking} 字符思考`);
    if (discardedRounds > 0) discardParts.push(`${discardedRounds} 个未完成工具调用`);
    const discardNote = discardParts.length > 0
      ? `，丢弃了 ${discardParts.join(" + ")}`
      : '';
    showToast(
      `从第 ${keptRounds.length} 轮工具调用后恢复${preserveNote}${discardNote}`,
      "info",
    );
  }

  // Save pre-checkpoint apiRounds & usage for merging after completion
  assistantMsg._continueApiRounds = (assistantMsg.apiRounds || []).slice();
  if (assistantMsg.usage)
    assistantMsg._continueUsage = { ...assistantMsg.usage };
  // Save the checkpoint toolRounds so we can merge with new ones
  assistantMsg._continueToolRounds = keptRounds.slice();
  // ★ Save modifiedFiles/modifiedFileList for merging after completion
  if (assistantMsg.modifiedFiles)
    assistantMsg._continueModifiedFiles = assistantMsg.modifiedFiles;
  if (assistantMsg.modifiedFileList)
    assistantMsg._continueModifiedFileList = (assistantMsg.modifiedFileList || []).slice();

  // ═══════════════════════════════════════════════════════════
  // ★ Step 4: Build messages — EXCLUDE the trailing assistant message
  // ═══════════════════════════════════════════════════════════
  // ★ Server-side message building: no longer send messages, backend loads from DB

  // ═══════════════════════════════════════════════════════════
  // ★ Step 5: Set up streaming UI
  // ═══════════════════════════════════════════════════════════
  if (activeConvId === conv.id) {
    // Re-render to show cleaned-up state (tool rounds only, no content)
    renderChat(conv, false);
    const lastIdx = conv.messages.length - 1;
    const msgEl = document.getElementById(`msg-${lastIdx}`);
    if (msgEl) {
      msgEl.id = "streaming-msg";
      const hdr = msgEl.querySelector(".message-header");
      if (hdr && !hdr.querySelector("#stream-elapsed-timer")) {
        const tmEl = document.createElement("span");
        tmEl.id = "stream-elapsed-timer";
        tmEl.className = "stream-elapsed-timer";
        hdr.appendChild(tmEl);
      }
      const relEl = msgEl.querySelector(".message-reltime");
      if (relEl) relEl.remove();
      const bodyEl = msgEl.querySelector(".message-body");
      if (bodyEl) {
        bodyEl.id = "streaming-body";
        bodyEl.innerHTML =
          '<div data-zone="tool"></div><div data-zone="thinking"></div><div data-zone="content"></div><div data-zone="status"><div class="stream-status"><div class="pulse"></div> Continuing…</div></div>';
        updateStreamingUI(assistantMsg);
      }
    }
    scrollToBottom();
  }

  // ═══════════════════════════════════════════════════════════
  // ★ Step 6: Build config payload
  //   Use _buildConvConfig() to get per-conv settings, avoiding the
  //   cross-talk bug where globals from the active conv leak into a
  //   background conv's continue request.
  //
  //   ★ Checkpoint assembly moved to the server (/api/chat/continue).
  //   The server-side _scan_continue_checkpoint() ports this same logic
  //   from the DB's assistant message — so we no longer ship
  //   `toolHistory`, `contentPrefix`, `checkpointToolRounds`,
  //   `checkpointUsage`, `checkpointApiRounds`, `checkpointModifiedFiles`
  //   or `checkpointModifiedFileList` on the wire.  The `_continueXxx`
  //   fields on `assistantMsg` stay local and are consumed by the SSE
  //   stream handlers to merge prior rounds with newly-streamed ones.
  // ═══════════════════════════════════════════════════════════
  const cfgPayload = _buildConvConfig(conv);
  debugLog(
    `Continue: delegating to /api/chat/continue with ${keptRounds.length} kept ` +
    `round(s), preservedContent=${preservedContent.length} chars`,
    "info",
  );

  // ★ Sync to server BEFORE POST so backend can load messages from DB.
  //   Note: the server's /api/chat/continue endpoint will itself roll back
  //   the DB state to the checkpoint before starting the task — but we
  //   still sync first so the server sees any local-only edits/attachments.
  await syncConversationToServer(conv);
  let taskId;
  try {
    const res = await fetch(apiUrl("/api/chat/continue"), {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        convId: conv.id,
        config: cfgPayload,
      }),
    });
    const data = await res.json();
    if (!res.ok) throw new Error(data.error || "Request failed");
    // ★ Server may tell us to fall back to a plain regenerate (no checkpoint
    //   available — e.g. all rounds incomplete). Honor it.
    if (data.fallback === 'regenerate') {
      debugLog(
        `Continue: server reports no recoverable checkpoint (${data.reason}); ` +
        `falling back to full regeneration`,
        "info",
      );
      // Undo the local rollback: restore content/thinking/rounds, pop the
      // trailing assistant and re-run the normal regenerate path.
      assistantMsg.content = originalContent;
      assistantMsg.thinking = "";
      assistantMsg.toolRounds = allRounds;
      delete assistantMsg._continueToolRounds;
      delete assistantMsg._continueContentPrefix;
      delete assistantMsg._continueApiRounds;
      delete assistantMsg._continueUsage;
      delete assistantMsg._continueModifiedFiles;
      delete assistantMsg._continueModifiedFileList;
      conv.messages.pop();
      conv._needsLoad = false;
      conv._serverMsgCount = conv.messages.length;
      if (activeConvId === conv.id) renderChat(conv, false);
      await syncConversationToServer(conv, { allowTruncate: true });
      await startAssistantResponse(conv.id);
      return;
    }
    taskId = data.taskId;
    if (data.checkpoint) {
      debugLog(
        `Continue: server checkpoint summary kept=${data.checkpoint.keptRounds} ` +
        `discarded=${data.checkpoint.discardedRounds} ` +
        `preservedContent=${data.checkpoint.preservedContentLen} ` +
        `discardedContent=${data.checkpoint.discardedContentLen}`,
        "info",
      );
    }
  } catch (e) {
    debugLog("Continue failed: " + e.message, "error");
    return;
  }
  conv.activeTaskId = taskId;
  saveConversations(conv.id);
  // No need to re-sync here — /api/chat/continue already persisted both the
  // rolled-back state AND the activeTaskId. A second PUT would just race
  // with the streaming task's checkpoints.
  connectToTask(conv.id, taskId);
}

/**
 * ★ Build a single tool history round from a batch of toolRound entries.
 * Each round represents one assistant message with tool_calls + their results.
 *
 * Optional per-provider continuity fields propagated through to the backend
 * when present — the Python backend (lib/tasks_pkg/message_builder.py) gates
 * them on the target model's actual API capability:
 *   • assistantContent   — text emitted alongside the tool calls
 *   • thinking           — reasoning trace (Claude extended-thinking)
 *   • thinkingSignature  — opaque signature for the thinking block (Claude)
 *   • toolCalls[i].extraContent — Gemini thought_signature envelope
 * Old DB rows without these fields round-trip harmlessly as plain calls.
 */
function _buildToolHistoryRound(batch) {
  const round = {
    assistantContent: "",
    toolCalls: [],
    toolResults: [],
  };
  // ★ Pick up per-round assistantContent + thinking from the first entry in
  //   the batch (tool_dispatch tags only the first entry per LLM round).
  for (const r of batch) {
    if (!round.assistantContent && r.assistantContent) {
      round.assistantContent = r.assistantContent;
    }
    if (!round.thinking && r.thinking) {
      round.thinking = r.thinking;
    }
    if (!round.thinkingSignature && r.thinkingSignature) {
      round.thinkingSignature = r.thinkingSignature;
    }
    const tc = {
      id: r.toolCallId,
      name: r.toolName,
      arguments: r.toolArgs || "{}",
    };
    // Gemini 3.x thought_signature — carried on the tool_call entry itself.
    if (r.extraContent) {
      tc.extraContent = r.extraContent;
    }
    round.toolCalls.push(tc);
    round.toolResults.push({
      tool_call_id: r.toolCallId,
      content: r.toolContent || "",
    });
  }
  return round;
}

// ── Toggles ──
function toggleThinking() {
  thinkingEnabled = !thinkingEnabled;
}
/* ★ Populate model dropdown dynamically from the registered models list.
 * Called once at startup from _loadServerConfigAndPopulate(). */
function _populateModelDropdown(models) {
  const dropdown = document.getElementById("presetDropdown");
  if (!dropdown || !models || models.length === 0) return;
  _registeredModels = models;
  dropdown.innerHTML = '';

  /* Filter out hidden models and non-chat models (but keep current model visible) */
  const visibleModels = models.filter(m => {
    if (m.model_id === config.model) return true;  // always keep current model
    if (_hiddenModels.has(m.model_id)) return false;
    var caps = m.capabilities || [];
    for (var i = 0; i < caps.length; i++) {
      if (caps[i] === 'image_gen' || caps[i] === 'embedding') return false;
    }
    return true;
  });

  /* Group models by provider (transit endpoint) */
  const grouped = {};  // provider_id → { name, models: [] }
  for (const m of visibleModels) {
    const pid = m.provider_id || 'default';
    if (!grouped[pid]) grouped[pid] = { name: m.provider_name || pid, models: [] };
    grouped[pid].models.push(m);
  }

  /* Render each provider group */
  const providerIds = Object.keys(grouped);
  for (const pid of providerIds) {
    const group = grouped[pid];
    /* Only show section headers when there are multiple providers */
    if (providerIds.length > 1) {
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
  fetch(apiUrl("/api/server-config"))
    .then(r => r.json())
    .then(data => {
      let models = data.dropdown_models;
      if (!models || models.length === 0) {
        /* Fallback: use the server model if available */
        models = serverModel ? [{ model_id: serverModel }] : [];
      }
      /* Build pricing cache from models data if available */
      if (data.model_pricing) {
        _modelPricingCache = data.model_pricing;
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
        var caps = m.capabilities || [];
        for (var i = 0; i < caps.length; i++) {
          if (caps[i] === 'image_gen' || caps[i] === 'embedding') return false;
        }
        return true;
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

      /* Re-apply model UI now that dropdown is populated */
      _applyModelUI(config.model || serverModel);

      /* ★ Auto-open settings if ?setup=1 (from bootstrap) or no API keys configured */
      _maybeAutoOpenSettings(data);
    })
    .catch(e => {
      console.warn('[_loadServerConfigAndPopulate] Failed:', e);
      /* Fallback with server model only */
      _populateModelDropdown(
        serverModel ? [{ model_id: serverModel }] : []
      );
      _applyModelUI(config.model || serverModel);
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
  // AI enhance: codeExec, memory, translate
  const aiCount = (codeExecEnabled ? 1 : 0) + (memoryEnabled ? 1 : 0) + (autoTranslate ? 1 : 0);
  const aiEl = document.getElementById("submenuAICount");
  if (aiEl) {
    aiEl.textContent = aiCount;
    aiEl.classList.toggle("visible", aiCount > 0);
  }
  const aiTrigger = document.querySelector("#submenuAI .submenu-trigger");
  if (aiTrigger) aiTrigger.classList.toggle("has-active", aiCount > 0);

  // Tools: browser, desktop, scheduler, image gen, human guidance
  const toolCount = (browserEnabled ? 1 : 0) + (desktopEnabled ? 1 : 0) + (schedulerEnabled ? 1 : 0) + (imageGenEnabled ? 1 : 0) + (humanGuidanceEnabled ? 1 : 0);
  const toolEl = document.getElementById("submenuToolsCount");
  if (toolEl) {
    toolEl.textContent = toolCount;
    toolEl.classList.toggle("visible", toolCount > 0);
  }
  const toolTrigger = document.querySelector("#submenuTools .submenu-trigger");
  if (toolTrigger) toolTrigger.classList.toggle("has-active", toolCount > 0);

  // Mode: swarm, endpoint
  const modeCount = (swarmEnabled ? 1 : 0) + (endpointEnabled ? 1 : 0);
  const modeEl = document.getElementById("submenuModeCount");
  if (modeEl) {
    modeEl.textContent = modeCount;
    modeEl.classList.toggle("visible", modeCount > 0);
  }
  const modeTrigger = document.querySelector("#submenuMode .submenu-trigger");
  if (modeTrigger) modeTrigger.classList.toggle("has-active", modeCount > 0);
}

function cycleSearchMode() {
  const modes = ["off", "single", "multi"];
  const idx = modes.indexOf(searchMode);
  _applySearchModeUI(modes[(idx + 1) % modes.length]);
  _saveConvToolState();
  debugLog(`Search: ${searchMode}`, "success");
}
function toggleBrowser() {
  // If not enabled yet and clicking to enable — open setup modal instead of just toggling
  if (!browserEnabled) {
    openBrowserModal();
    return;
  }
  // If already enabled — just toggle off
  _applyBrowserUI(false);
  _saveConvToolState();
  debugLog("Browser Bridge: OFF", "success");
}
function toggleBrowserFromModal() {
  _applyBrowserUI(!browserEnabled);
  _saveConvToolState();
  updateSubmenuCounts();
  debugLog(`Browser Bridge: ${browserEnabled ? "ON" : "OFF"}`, "success");
  if (browserEnabled) closeBrowserModal();
}
function openBrowserModal() {
  document.getElementById("browserModal").classList.add("open");
  _checkBrowserStatus();
  _updateBrowserModalBtn();
}
function closeBrowserModal() {
  document.getElementById("browserModal").classList.remove("open");
}
function _updateBrowserModalBtn() {
  const btn = document.getElementById("browserModalToggleBtn");
  if (!btn) return;
  btn.textContent = browserEnabled
    ? "Disable Browser Bridge"
    : "Enable Browser Bridge";
  btn.className = browserEnabled ? "btn btn-secondary" : "btn btn-primary";
}
async function _checkBrowserStatus() {
  const dot = document.querySelector(
    "#browserStatusIndicator .browser-status-dot",
  );
  const txt = document.querySelector(
    "#browserStatusIndicator .browser-status-text",
  );
  const badge = document.getElementById("browserBadge");
  try {
    const r = await fetch(apiUrl("/api/browser/status"));
    const d = await r.json();
    if (d.connected) {
      dot?.classList.replace("disconnected", "connected") ||
        dot?.classList.add("connected");
      dot?.classList.remove("disconnected");
      /* ★ Per-client routing: capture the first connected client's ID.
       * This ID is sent with every task so commands are routed to the
       * correct device's extension, not a random one. */
      const clients = d.clients || [];
      const clientCount = clients.length;
      if (clientCount > 0) {
        /* Use the first connected client (most recently active) */
        const activeClient = clients[0];
        window._browserClientId = activeClient.client_id;
        const shortId = activeClient.client_id.substring(0, 8);
        txt &&
          (txt.textContent = clientCount > 1
            ? `${clientCount} extensions connected (using ${shortId}…)`
            : `Extension connected (${shortId}…, ${d.secondsAgo}s ago)`);
      } else {
        txt &&
          (txt.textContent = `Extension connected (${d.secondsAgo}s ago)`);
      }
      badge?.classList.remove("disconnected");
    } else {
      dot?.classList.replace("connected", "disconnected") ||
        dot?.classList.add("disconnected");
      dot?.classList.remove("connected");
      window._browserClientId = null;
      txt &&
        (txt.textContent =
          "Extension not connected — follow setup steps below");
      badge?.classList.add("disconnected");
    }
  } catch (e) {
    dot?.classList.replace("connected", "disconnected");
    txt && (txt.textContent = "Cannot reach server");
  }
}
function downloadBrowserExtension() {
  window.open(apiUrl("/api/browser/download"), "_blank");
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
  _saveConvToolState();
  debugLog(
    endpointEnabled
      ? "Endpoint Mode: ON — Planner → Worker → Critic autonomous loop (max 10 iterations)"
      : "Endpoint Mode: OFF",
    "success",
  );
}

/* ═══ Folder management UI ═══ */

/** Show a dropdown under the folder-assign button to pick or create a folder */
function _showFolderPicker(convId, anchorEl) {
  // Remove any existing picker
  _closeFolderPicker();
  const conv = conversations.find(c => c.id === convId);
  if (!conv) return;

  const folders = typeof getFolders === 'function' ? getFolders() : [];
  const picker = document.createElement('div');
  picker.className = 'folder-picker';
  picker.id = '_folderPicker';

  let html = `<div class="folder-picker-title">${t('folder.moveToFolder')}</div>`;
  // "Remove from folder" option if currently in a folder
  if (conv.folderId) {
    html += `<div class="folder-picker-item folder-picker-remove" data-folder-id="">` +
      `<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><line x1="18" y1="6" x2="6" y2="18"/><line x1="6" y1="6" x2="18" y2="18"/></svg>` +
      `${t('folder.removeFromFolder')}</div>`;
  }
  // Existing folders
  for (const f of folders) {
    const active = conv.folderId === f.id ? ' active' : '';
    const dot = f.color ? `<span class="folder-color-dot" style="background:${escapeHtml(f.color)}"></span>` : '';
    html += `<div class="folder-picker-item${active}" data-folder-id="${escapeHtml(f.id)}">${dot}${escapeHtml(f.name)}</div>`;
  }
  // "New folder" option
  html += `<div class="folder-picker-divider"></div>`;
  html += `<div class="folder-picker-item folder-picker-new">` +
    `<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round"><line x1="12" y1="5" x2="12" y2="19"/><line x1="5" y1="12" x2="19" y2="12"/></svg>` +
    `${t('folder.newFolder')}</div>`;

  picker.innerHTML = html;

  // Position near the anchor button
  document.body.appendChild(picker);
  const rect = anchorEl.getBoundingClientRect();
  picker.style.top = Math.min(rect.bottom + 4, window.innerHeight - picker.offsetHeight - 8) + 'px';
  picker.style.left = Math.min(rect.left, window.innerWidth - picker.offsetWidth - 8) + 'px';

  // Handle clicks
  picker.addEventListener('click', async (ev) => {
    ev.stopPropagation();
    const item = ev.target.closest('.folder-picker-item');
    if (!item) return;

    if (item.classList.contains('folder-picker-new')) {
      _closeFolderPicker();
      _promptCreateFolder(convId);
      return;
    }
    const folderId = item.dataset.folderId;
    setConversationFolder(convId, folderId || null);
    _closeFolderPicker();
    if (folderId) {
      const f = getFolderById(folderId);
      if (typeof showToast === 'function') showToast('', t('folder.movedToFolder'), f ? f.name : '', 2000);
    } else {
      if (typeof showToast === 'function') showToast('', t('folder.removedFromFolder'), '', 2000);
    }
  });

  // Close on outside click
  setTimeout(() => {
    document.addEventListener('click', _closeFolderPicker, { once: true });
  }, 0);
}

function _closeFolderPicker() {
  const p = document.getElementById('_folderPicker');
  if (p) p.remove();
}

/** Show inline dialog to create a new folder, optionally assigning a conv to it */
function _promptCreateFolder(assignConvId) {
  // Remove any existing dialog
  const existing = document.getElementById('_folderCreateDialog');
  if (existing) existing.remove();

  const colors = ['#6e56cf', '#3b82f6', '#10b981', '#f59e0b', '#ef4444', '#ec4899', '#8b5cf6', '#14b8a6'];
  let selectedColor = colors[Math.floor(Math.random() * colors.length)];

  const overlay = document.createElement('div');
  overlay.id = '_folderCreateDialog';
  overlay.className = 'folder-dialog-overlay';
  overlay.innerHTML = `
    <div class="folder-dialog">
      <div class="folder-dialog-title">${t('folder.createTitle')}</div>
      <input type="text" class="folder-dialog-input" id="_folderNameInput"
             placeholder="${t('folder.namePh')}" maxlength="50" autocomplete="off" spellcheck="false">
      <div class="folder-dialog-colors" id="_folderColorPicker">
        ${colors.map(c => `<span class="folder-color-dot${c === selectedColor ? ' selected' : ''}" data-color="${c}" style="background:${c}"></span>`).join('')}
      </div>
      <div class="folder-dialog-actions">
        <button class="folder-dialog-cancel" id="_folderDialogCancel">${t('folder.cancel')}</button>
        <button class="folder-dialog-ok" id="_folderDialogOk">${t('folder.create')}</button>
      </div>
    </div>
  `;
  document.body.appendChild(overlay);

  const nameInput = document.getElementById('_folderNameInput');
  const colorPicker = document.getElementById('_folderColorPicker');
  const okBtn = document.getElementById('_folderDialogOk');
  const cancelBtn = document.getElementById('_folderDialogCancel');

  // Focus input
  setTimeout(() => nameInput.focus(), 50);

  // Color selection
  colorPicker.addEventListener('click', (e) => {
    const dot = e.target.closest('.folder-color-dot');
    if (!dot) return;
    colorPicker.querySelectorAll('.folder-color-dot').forEach(d => d.classList.remove('selected'));
    dot.classList.add('selected');
    selectedColor = dot.dataset.color;
  });

  function _closeDialog() { overlay.remove(); }

  async function _submit() {
    const name = nameInput.value.trim();
    if (!name) { nameInput.focus(); return; }
    okBtn.disabled = true;
    okBtn.textContent = t('folder.creating');
    const folder = await createFolder(name, selectedColor);
    _closeDialog();
    if (!folder) {
      if (typeof showToast === 'function') showToast('', t('folder.createFailed'), t('folder.cannotCreate'), 3000);
      return;
    }
    if (assignConvId) {
      setConversationFolder(assignConvId, folder.id);
    }
    renderConversationList();
    if (typeof showToast === 'function') showToast('', t('folder.created'), folder.name, 2000);
  }

  okBtn.addEventListener('click', _submit);
  cancelBtn.addEventListener('click', _closeDialog);
  overlay.addEventListener('click', (e) => { if (e.target === overlay) _closeDialog(); });
  nameInput.addEventListener('keydown', (e) => {
    if (e.key === 'Enter') { e.preventDefault(); _submit(); }
    if (e.key === 'Escape') _closeDialog();
  });
}

function _promptRenameFolder(folderId) {
  const f = getFolderById(folderId);
  if (!f) return;

  // Remove any existing dialog
  const existing = document.getElementById('_folderCreateDialog');
  if (existing) existing.remove();

  const overlay = document.createElement('div');
  overlay.id = '_folderCreateDialog';
  overlay.className = 'folder-dialog-overlay';
  overlay.innerHTML = `
    <div class="folder-dialog">
      <div class="folder-dialog-title">${t('folder.renameTitle')}</div>
      <input type="text" class="folder-dialog-input" id="_folderNameInput"
             placeholder="${t('folder.namePh')}" maxlength="50" autocomplete="off" spellcheck="false">
      <div class="folder-dialog-actions">
        <button class="folder-dialog-cancel" id="_folderDialogCancel">${t('folder.cancel')}</button>
        <button class="folder-dialog-ok" id="_folderDialogOk">${t('folder.ok')}</button>
      </div>
    </div>
  `;
  document.body.appendChild(overlay);

  const nameInput = document.getElementById('_folderNameInput');
  nameInput.value = f.name;
  setTimeout(() => { nameInput.focus(); nameInput.select(); }, 50);

  function _closeDialog() { overlay.remove(); }

  async function _submit() {
    const name = nameInput.value.trim();
    if (!name || name === f.name) { _closeDialog(); return; }
    await updateFolder(folderId, { name });
    _closeDialog();
    renderConversationList();
  }

  document.getElementById('_folderDialogOk').addEventListener('click', _submit);
  document.getElementById('_folderDialogCancel').addEventListener('click', _closeDialog);
  overlay.addEventListener('click', (e) => { if (e.target === overlay) _closeDialog(); });
  nameInput.addEventListener('keydown', (e) => {
    if (e.key === 'Enter') { e.preventDefault(); _submit(); }
    if (e.key === 'Escape') _closeDialog();
  });
}

function _confirmDeleteFolder(folderId) {
  const f = getFolderById(folderId);
  if (!f) return;

  const existing = document.getElementById('_folderCreateDialog');
  if (existing) existing.remove();

  const overlay = document.createElement('div');
  overlay.id = '_folderCreateDialog';
  overlay.className = 'folder-dialog-overlay';
  overlay.innerHTML = `
    <div class="folder-dialog">
      <div class="folder-dialog-title">${t('folder.deleteTitle')}</div>
      <div style="font-size:13px;color:var(--text-secondary);margin-bottom:16px;line-height:1.5">
        ${t('folder.deleteConfirm')} <b style="color:var(--text-primary)">${f.name}</b>？<br>
        ${t('folder.deleteHint')}
      </div>
      <div class="folder-dialog-actions">
        <button class="folder-dialog-cancel" id="_folderDialogCancel">${t('folder.cancel')}</button>
        <button class="folder-dialog-ok" id="_folderDialogOk" style="background:#ef4444">${t('common.delete')}</button>
      </div>
    </div>
  `;
  document.body.appendChild(overlay);

  function _closeDialog() { overlay.remove(); }

  document.getElementById('_folderDialogOk').addEventListener('click', async () => {
    _closeDialog();
    // If we're viewing this folder, exit folder view first
    if (typeof getActiveFolderId === 'function' && getActiveFolderId() === folderId) {
      setActiveFolderId(null);
    }
    await deleteFolder(folderId);
    renderConversationList();
    if (typeof showToast === 'function') showToast('', t('folder.deleted'), f.name, 2000);
  });
  document.getElementById('_folderDialogCancel').addEventListener('click', _closeDialog);
  overlay.addEventListener('click', (e) => { if (e.target === overlay) _closeDialog(); });
}

/** Initialize folder tab bar interactions */
function _initFolderTabs() {
  const tabsEl = document.getElementById('folderTabs');
  if (!tabsEl) return;

  // ── Expand/collapse toggle for wrapped folder tabs ──
  tabsEl.addEventListener('click', (e) => {
    const toggle = e.target.closest('.folder-tabs-toggle');
    if (!toggle) return;
    e.stopPropagation();
    const scrollEl = tabsEl.querySelector('.folder-tabs-scroll');
    if (!scrollEl) return;
    _folderTabsExpanded = !_folderTabsExpanded;
    scrollEl.classList.toggle('expanded', _folderTabsExpanded);
    // Update label
    const label = toggle.querySelector('.folder-tabs-toggle-label');
    if (label) {
      if (_folderTabsExpanded) {
        label.textContent = t('sidebar.lessFolders');
      } else {
        // Count hidden folder tabs (exclude the "+" add button)
        const collapsedMax = 94; // matches CSS max-height (3 rows)
        const tabs = scrollEl.querySelectorAll('.folder-tab:not(.folder-tab-add)');
        let hidden = 0;
        tabs.forEach(tab => {
          if (tab.offsetTop + tab.offsetHeight > collapsedMax) hidden++;
        });
        label.textContent = hidden > 0 ? `+${hidden}` : '';
      }
    }
  });

  // Click: switch folder tab or create new folder
  tabsEl.addEventListener('click', (e) => {
    const tab = e.target.closest('.folder-tab');
    if (!tab) return;
    e.stopPropagation();
    if (tab.classList.contains('folder-tab-add')) {
      _promptCreateFolder(null);
      return;
    }
    const folderId = tab.dataset.folderId;
    setActiveFolderId(folderId || null);
  });

  // Right-click / context menu on folder tabs (rename/delete)
  tabsEl.addEventListener('contextmenu', (e) => {
    const tab = e.target.closest('.folder-tab');
    if (!tab || tab.classList.contains('folder-tab-add') || !tab.dataset.folderId) return;
    e.preventDefault();
    e.stopPropagation();
    _showFolderTabMenu(tab.dataset.folderId, e.clientX, e.clientY);
  });

  // Drag-and-drop: allow dragging conversations onto folder tabs
  tabsEl.addEventListener('dragover', (e) => {
    if (!_dragConvId) return;
    const tab = e.target.closest('.folder-tab');
    if (!tab || tab.classList.contains('folder-tab-add')) return;
    e.preventDefault();
    e.dataTransfer.dropEffect = 'move';
    tabsEl.querySelectorAll('.folder-tab').forEach(t => t.classList.remove('folder-tab-drop'));
    tab.classList.add('folder-tab-drop');
  });

  tabsEl.addEventListener('dragleave', (e) => {
    const tab = e.target.closest('.folder-tab');
    if (tab) tab.classList.remove('folder-tab-drop');
  });

  tabsEl.addEventListener('drop', (e) => {
    e.preventDefault();
    const convId = _dragConvId || e.dataTransfer.getData('text/plain');
    if (!convId) return;
    const tab = e.target.closest('.folder-tab');
    if (!tab || tab.classList.contains('folder-tab-add')) return;
    const folderId = tab.dataset.folderId || null;
    tabsEl.querySelectorAll('.folder-tab').forEach(t => t.classList.remove('folder-tab-drop'));
    setConversationFolder(convId, folderId);
    const f = folderId ? getFolderById(folderId) : null;
    if (f) {
      if (typeof showToast === 'function') showToast('', '已移入文件夹', f.name, 2000);
    } else if (!folderId) {
      if (typeof showToast === 'function') showToast('', t('folder.removedFromFolder'), '', 2000);
    }
  });
}

/** Show context menu for a folder tab (rename/delete) */
function _showFolderTabMenu(folderId, x, y) {
  // Remove existing
  const old = document.getElementById('_folderTabMenu');
  if (old) old.remove();

  const f = getFolderById(folderId);
  if (!f) return;

  const menu = document.createElement('div');
  menu.id = '_folderTabMenu';
  menu.className = 'folder-tab-menu';
  menu.innerHTML = `
    <div class="folder-tab-menu-item" data-action="rename">
      <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M17 3a2.828 2.828 0 1 1 4 4L7.5 20.5 2 22l1.5-5.5L17 3z"/></svg>
      ${t('folder.rename')}
    </div>
    <div class="folder-tab-menu-item folder-tab-menu-delete" data-action="delete">
      <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><polyline points="3 6 5 6 21 6"/><path d="M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6m3 0V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2"/></svg>
      ${t('folder.deleteAction')}
    </div>
  `;
  document.body.appendChild(menu);

  // Position
  const mw = menu.offsetWidth, mh = menu.offsetHeight;
  menu.style.left = Math.min(x, window.innerWidth - mw - 8) + 'px';
  menu.style.top = Math.min(y, window.innerHeight - mh - 8) + 'px';

  menu.addEventListener('click', (e) => {
    const item = e.target.closest('.folder-tab-menu-item');
    if (!item) return;
    menu.remove();
    if (item.dataset.action === 'rename') _promptRenameFolder(folderId);
    else if (item.dataset.action === 'delete') _confirmDeleteFolder(folderId);
  });

  // Close on outside click
  setTimeout(() => {
    document.addEventListener('click', function _close() {
      menu.remove();
      document.removeEventListener('click', _close);
    }, { once: true });
  }, 0);
}

/* ── Drag-and-drop conversations — drag from convList, drop on folder tabs ── */
let _dragConvId = null;
function _initFolderDragDrop() {
  const convList = document.getElementById('convList');
  if (!convList) return;

  function _onDragStart(e) {
    const item = e.target.closest('.conv-item');
    if (!item || !item.dataset.convId) return;
    _dragConvId = item.dataset.convId;
    e.dataTransfer.effectAllowed = 'move';
    e.dataTransfer.setData('text/plain', item.dataset.convId);
    item.classList.add('conv-dragging');
    // Highlight folder tabs as drop targets
    setTimeout(() => {
      const tabsEl = document.getElementById('folderTabs');
      if (tabsEl) tabsEl.classList.add('folder-tabs-drop-active');
    }, 0);
  }

  function _onDragEnd() {
    _dragConvId = null;
    document.querySelectorAll('.conv-dragging').forEach(el => el.classList.remove('conv-dragging'));
    const tabsEl = document.getElementById('folderTabs');
    if (tabsEl) {
      tabsEl.classList.remove('folder-tabs-drop-active');
      tabsEl.querySelectorAll('.folder-tab-drop').forEach(t => t.classList.remove('folder-tab-drop'));
    }
  }

  convList.addEventListener('dragstart', _onDragStart);
  convList.addEventListener('dragend', _onDragEnd);
}


function toggleSidebar() {
  const sidebar = document.getElementById("sidebar");
  const backdrop = document.getElementById("sidebarBackdrop");
  sidebar.classList.toggle("collapsed");
  // Mobile only: show/hide backdrop overlay
  if (backdrop) {
    const isMobile = window.innerWidth <= 768;
    const isOpen = !sidebar.classList.contains("collapsed");
    backdrop.classList.toggle("visible", isMobile && isOpen);
  }
  /* Sidebar width change affects available space for toolbar */
  setTimeout(_scheduleReflow, 250);  /* after sidebar transition finishes */
}

/* ── Mobile: auto-collapse sidebar on load ── */
(function initMobileLayout() {
  if (window.innerWidth <= 768) {
    const sidebar = document.getElementById("sidebar");
    if (sidebar && !sidebar.classList.contains("collapsed")) {
      sidebar.classList.add("collapsed");
    }
  }
})();

/* ═══ Mobile "More" Bottom Sheet ═══
 * Mirrors the state of the desktop toolbar toggles (codeExec, memory,
 * translate, browser, imageGen, humanGuidance, swarm, endpoint).
 * Each item reads the live state from the existing toggle elements. */

function toggleMobileSheet() {
  const sheet = document.getElementById("mobileSheet");
  const backdrop = document.getElementById("mobileSheetBackdrop");
  if (!sheet) return;
  const isOpen = sheet.classList.contains("open");
  if (isOpen) {
    closeMobileSheet();
  } else {
    updateMobileSheet();
    sheet.classList.add("open");
    if (backdrop) backdrop.classList.add("open");
  }
}

function closeMobileSheet() {
  const sheet = document.getElementById("mobileSheet");
  const backdrop = document.getElementById("mobileSheetBackdrop");
  if (sheet) sheet.classList.remove("open");
  if (backdrop) backdrop.classList.remove("open");
}

function updateMobileSheet() {
  /* Sync mobile backend selector with active backend */
  _updateMobileBackendSection();
  /* Sync each mobile sheet item's .active class with the desktop toggle state */
  const map = {
    mobileCodeExec:    "codeExecToggle",
    mobileMemory:      "memoryToggle",
    mobileTranslate:   "translateToggle",
    mobileBrowser:     "browserToggle",
    mobileImageGen:    "imageGenToggle",
    mobileHumanGuidance: "humanGuidanceToggle",
    mobileSwarm:       "swarmToggle",
    mobileEndpoint:    "endpointToggle"
  };
  let activeCount = 0;
  for (const [mobileId, desktopId] of Object.entries(map)) {
    const mobileEl = document.getElementById(mobileId);
    const desktopEl = document.getElementById(desktopId);
    if (!mobileEl || !desktopEl) continue;
    const isActive = desktopEl.classList.contains("active");
    mobileEl.classList.toggle("active", isActive);
    if (isActive) activeCount++;
  }
  /* Update the "more" button to show if any toggles are active */
  const moreBtn = document.getElementById("mobileMoreBtn");
  if (moreBtn) moreBtn.classList.toggle("has-active", activeCount > 0);
  /* Also update desktop submenu counts (they still exist in DOM) */
  if (typeof updateSubmenuCounts === "function") updateSubmenuCounts();
  /* Sync mobile depth section visibility + active state */
  updateMobileDepth();
}

/**
 * Sync the mobile bottom sheet depth bar with the desktop depth bar.
 * Shows/hides the section based on whether the model supports thinking depth.
 */
function updateMobileDepth() {
  const desktopBar = document.getElementById("thinkingDepthSection");
  const mobileSection = document.getElementById("mobileDepthSection");
  if (!mobileSection) return;
  /* Show mobile depth section when desktop depth bar has display set to 'flex' by JS
   * (on mobile, CSS hides it with display:none!important, but JS still sets .style.display) */
  const isVisible = desktopBar && (desktopBar.style.display === "flex" || desktopBar.style.display === "");
  mobileSection.style.display = isVisible ? "" : "none";
  if (!isVisible) return;
  /* Sync active button state */
  const activeDesktop = desktopBar.querySelector(".depth-btn.active");
  const activeDepth = activeDesktop ? activeDesktop.dataset.depth : "medium";
  mobileSection.querySelectorAll(".mobile-depth-btn").forEach(btn => {
    btn.classList.toggle("active", btn.dataset.depth === activeDepth);
  });
}

/**
 * Switch backend from mobile bottom sheet.
 * Calls the existing switchAgentBackend() and updates the sheet UI.
 */
async function switchMobileBackend(backendName) {
  await switchAgentBackend(backendName);
  _updateMobileBackendSection();
}

/**
 * Sync the mobile backend section's active states and availability.
 * Fetches backend status if not cached.
 */
async function _updateMobileBackendSection() {
  const backends = _agentBackendCache || await _fetchAgentBackends();
  // Sync active state
  document.querySelectorAll('#mobileBackendSection .mobile-sheet-item').forEach(el => {
    const bn = el.dataset.backend;
    el.classList.toggle('active', bn === activeAgentBackend);
  });
  // Sync availability for non-builtin backends
  for (const b of backends) {
    if (b.name === 'builtin') continue;
    const mobileEl = b.name === 'claude-code'
      ? document.getElementById('mobileBackendClaudeCode')
      : document.getElementById('mobileBackendCodex');
    const statusEl = b.name === 'claude-code'
      ? document.getElementById('ccStatusMobile')
      : document.getElementById('codexStatusMobile');
    if (!mobileEl) continue;
    const usable = b.available && b.authenticated;
    mobileEl.classList.toggle('disabled', !usable);
    mobileEl.style.opacity = usable ? '' : '0.45';
    if (statusEl) {
      if (!b.available) {
        statusEl.textContent = t('agent.notInstalled');
        statusEl.style.color = 'var(--text-tertiary)';
      } else if (!b.authenticated) {
        statusEl.textContent = t('agent.notAuthenticated');
        statusEl.style.color = '#f59e0b';
      } else {
        statusEl.textContent = b.version || t('agent.ready');
        statusEl.style.color = '#22c55e';
      }
    }
  }
}

/* ── Reflow toolbar on window resize ── */
window.addEventListener('resize', (function() {
  let tid;
  return function() {
    clearTimeout(tid);
    tid = setTimeout(function() {
      _scheduleReflow();
      /* Hide mobile backdrop if resized past mobile breakpoint */
      if (window.innerWidth > 768) {
        const bd = document.getElementById('sidebarBackdrop');
        if (bd) bd.classList.remove('visible');
      }
    }, 120);
  };
})());

/* ── Mobile: swipe-to-open sidebar + swipe-to-close ── */
(function initMobileGestures() {
  if (!("ontouchstart" in window)) return;

  let touchStartX = 0, touchStartY = 0, touchDelta = 0, tracking = false, direction = null;
  const EDGE_WIDTH = 30;       // px from left edge to start tracking
  const SWIPE_THRESHOLD = 60;  // px to confirm a swipe

  document.addEventListener("touchstart", function(e) {
    const t = e.touches[0];
    touchStartX = t.clientX;
    touchStartY = t.clientY;
    touchDelta = 0;
    direction = null;
    const sidebar = document.getElementById("sidebar");
    const isCollapsed = sidebar.classList.contains("collapsed");
    // Track: swipe from left edge to open, or swipe on open sidebar/backdrop to close
    if (isCollapsed && touchStartX < EDGE_WIDTH) {
      tracking = true;
    } else if (!isCollapsed) {
      tracking = true;
    }
  }, { passive: true });

  document.addEventListener("touchmove", function(e) {
    if (!tracking) return;
    const t = e.touches[0];
    const dx = t.clientX - touchStartX;
    const dy = t.clientY - touchStartY;
    // Lock direction on first significant move
    if (!direction) {
      if (Math.abs(dx) > 8 || Math.abs(dy) > 8) {
        direction = Math.abs(dx) > Math.abs(dy) ? "horizontal" : "vertical";
      }
    }
    if (direction === "horizontal") {
      touchDelta = dx;
    }
  }, { passive: true });

  document.addEventListener("touchend", function(e) {
    if (!tracking || direction !== "horizontal") {
      tracking = false;
      return;
    }
    const sidebar = document.getElementById("sidebar");
    const isCollapsed = sidebar.classList.contains("collapsed");
    if (isCollapsed && touchDelta > SWIPE_THRESHOLD) {
      // Swipe right from edge → open
      toggleSidebar();
    } else if (!isCollapsed && touchDelta < -SWIPE_THRESHOLD) {
      // Swipe left → close
      toggleSidebar();
    }
    tracking = false;
  }, { passive: true });

  // Mobile: close submenus/dropdowns when tapping outside
  document.addEventListener("click", function(e) {
    if (window.innerWidth > 768) return;
    // Close open toolbar submenus
    document.querySelectorAll(".toolbar-submenu.open").forEach(sub => {
      if (!sub.contains(e.target)) sub.classList.remove("open");
    });
    // Close preset dropdown
    const pw = document.querySelector(".preset-toggle-wrapper.open");
    if (pw && !pw.contains(e.target)) pw.classList.remove("open");
  });
})();

/* ── Mobile: auto-collapse sidebar on conversation select ── */
(function patchMobileConvSelect() {
  // After the page loads, intercept conversation clicks
  document.addEventListener("click", function(e) {
    if (window.innerWidth > 768) return;
    const convItem = e.target.closest(".conv-item");
    if (convItem) {
      // Let the real click handler fire first, then close sidebar
      setTimeout(() => {
        const sidebar = document.getElementById("sidebar");
        if (sidebar && !sidebar.classList.contains("collapsed")) {
          toggleSidebar();
        }
      }, 150);
    }
  });
})();

/* ── Mobile: handle virtual keyboard resize via visualViewport API ── */
(function initMobileKeyboardHandler() {
  if (!window.visualViewport) return;
  /* On mobile, when the virtual keyboard opens the visual viewport shrinks.
   * We adjust body height to match, keeping the input area visible. */
  let lastHeight = 0;
  /* Track whether user was near bottom BEFORE keyboard starts closing.
   * We sample this on every resize so we know the state at keyboard-open time. */
  let _wasNearBottom = true;
  function onViewportResize() {
    if (window.innerWidth > 768) return;
    const vv = window.visualViewport;
    const newH = vv.height;
    if (Math.abs(newH - lastHeight) < 1) return;
    const growing = newH > lastHeight;
    lastHeight = newH;
    /* Set explicit height on body to match the visual viewport */
    document.body.style.height = newH + 'px';
    if (!growing) {
      /* Keyboard opening (viewport shrinking) — record scroll state and
       * scroll textarea into view */
      _wasNearBottom = isNearBottom(200);
      const ta = document.getElementById('userInput');
      if (ta && document.activeElement === ta) {
        requestAnimationFrame(function() {
          ta.scrollIntoView({ block: 'end', behavior: 'smooth' });
        });
      }
    } else {
      /* ★ Keyboard closing (viewport growing) — the viewport height increases
       * but scrollTop stays the same, so chat appears to "jump to the middle".
       * Re-scroll to bottom if the user was near bottom before, or if there's
       * an active stream (user just sent a message and is watching the reply).
       * Force sync reflow first (void scrollHeight) so the layout reflects
       * the new body height, then scroll immediately + schedule a safety rAF. */
      if (_wasNearBottom || (typeof activeStreams !== 'undefined' && activeStreams.size > 0)) {
        var cc = document.getElementById('chatContainer');
        if (cc) {
          void cc.scrollHeight; // force reflow after body height change
          cc.scrollTop = cc.scrollHeight;
        }
        /* Safety: keyboard dismiss can fire multiple resize events as it
         * animates closed — schedule another scroll after layout settles. */
        requestAnimationFrame(function() {
          scrollToBottom(true);
        });
      }
    }
  }
  window.visualViewport.addEventListener('resize', onViewportResize);
  /* Reset on blur / keyboard dismiss */
  window.visualViewport.addEventListener('scroll', function() {
    if (window.innerWidth > 768) return;
    document.body.style.height = window.visualViewport.height + 'px';
  });
})();

function handleKeyDown(e) {
  // Enter (no modifier) → send; Ctrl+Enter → newline
  if (e.key === "Enter" && !e.ctrlKey && !e.shiftKey) {
    e.preventDefault();
    // Route to image generation when in image gen mode
    if (imageGenMode) { generateImageDirect(); }
    else { sendMessage(); }
    return;
  }
  if (e.key === "Enter" && e.ctrlKey) {
    e.preventDefault();
    const ta = e.target;
    const start = ta.selectionStart;
    const end = ta.selectionEnd;
    ta.value = ta.value.substring(0, start) + '\n' + ta.value.substring(end);
    ta.selectionStart = ta.selectionEnd = start + 1;
    ta.dispatchEvent(new Event('input', { bubbles: true }));
    return;
  }
  // Ctrl+Shift+K — wrap selected text in <notranslate> tags (skip translation)
  if (e.key === "K" && e.ctrlKey && e.shiftKey) {
    e.preventDefault();
    _wrapSelectionNoTranslate(e.target);
    return;
  }
  // Escape exits image gen mode or branch mode
  if (e.key === "Escape") {
    if (imageGenMode) { e.preventDefault(); exitImageGenMode(); return; }
    if (typeof isBranchModeActive === "function" && isBranchModeActive()) {
      e.preventDefault();
      closeBranchPanel();
      return;
    }
  }
}

/**
 * Wrap the selected text in the textarea with <notranslate> tags.
 * If no text is selected, insert an empty <notranslate></notranslate> pair
 * with cursor positioned in the middle.
 */
function _wrapSelectionNoTranslate(textarea) {
  if (!textarea || textarea.tagName !== 'TEXTAREA') return;
  const start = textarea.selectionStart;
  const end = textarea.selectionEnd;
  const text = textarea.value;
  const selected = text.substring(start, end);
  const tag = '<notranslate>';
  const closeTag = '</notranslate>';
  const before = text.substring(0, start);
  const after = text.substring(end);
  textarea.value = before + tag + selected + closeTag + after;
  // Position cursor: if had selection, select the wrapped text; else put cursor inside tags
  if (selected) {
    textarea.selectionStart = start + tag.length;
    textarea.selectionEnd = start + tag.length + selected.length;
  } else {
    textarea.selectionStart = textarea.selectionEnd = start + tag.length;
  }
  textarea.focus();
  // Trigger auto-resize
  textarea.dispatchEvent(new Event('input', { bubbles: true }));
}


// ══════════════════════════════════════════════════════
//  Settings — Tabbed Panel
// ══════════════════════════════════════════════════════

// _serverConfig is declared in settings.js

// ── Settings functions moved to settings.js ──


// ══════════════════════════════════════════════════════
//  Theme System
// ══════════════════════════════════════════════════════
const _THEMES = ["dark", "light", "tofu"];
const _THEME_ICONS = {
  dark: "🌙",
  light: "☀️",
  tofu: "🍮",
};

function _getCurrentTheme() {
  return localStorage.getItem("claude_ui_theme") || "tofu";
}
function applyTheme(theme) {
  if (!_THEMES.includes(theme)) theme = "tofu";
  /* Always set data-theme attribute — no special "no-attribute" default */
  document.documentElement.setAttribute("data-theme", theme);
  localStorage.setItem("claude_ui_theme", theme);
  // Update cycle button icon
  const btn = document.getElementById("themeCycleBtn");
  if (btn) {
    const sp = btn.querySelector("span");
    if (sp) sp.textContent = _THEME_ICONS[theme] || "🌙";
  }
  // Update picker in settings modal
  document.querySelectorAll(".theme-option").forEach((el) => {
    el.classList.toggle("active", el.dataset.theme === theme);
  });
  debugLog(`Theme → ${theme}`, "success");
}
function selectTheme(theme) {
  applyTheme(theme);
}
function cycleTheme() {
  const cur = _getCurrentTheme();
  const idx = _THEMES.indexOf(cur);
  applyTheme(_THEMES[(idx + 1) % _THEMES.length]);
}

/* Cost dashboard aliases — moved to myday.js */

// ── Sidebar search (expandable from header button) ──
function initSidebarSearch() {
  const input = document.getElementById("sidebarSearchInput");
  let timer = null;
  input.addEventListener("input", () => {
    clearTimeout(timer);
    timer = setTimeout(() => {
      sidebarSearchQuery = input.value.trim().toLowerCase();
      /* Exit folder view when searching — search should cover all conversations */
      if (sidebarSearchQuery && typeof getActiveFolderId === 'function' && getActiveFolderId()) {
        setActiveFolderId(null);
      }
      renderConversationList();
    }, 300);
  });
  input.addEventListener("keydown", (e) => {
    if (e.key === "Escape") {
      e.preventDefault();
      closeSidebarSearch();
    }
  });
}

/** Toggle sidebar search panel open/closed */
function toggleSidebarSearch() {
  const wrapper = document.getElementById("sidebarSearchWrapper");
  const toggle = document.getElementById("sidebarSearchToggle");
  if (!wrapper) return;
  const isOpen = wrapper.style.display !== "none";
  if (isOpen) {
    closeSidebarSearch();
  } else {
    wrapper.style.display = "";
    if (toggle) toggle.classList.add("active");
    const input = document.getElementById("sidebarSearchInput");
    if (input) { input.focus(); input.select(); }
  }
}

/** Close sidebar search and clear results */
function closeSidebarSearch() {
  const wrapper = document.getElementById("sidebarSearchWrapper");
  const toggle = document.getElementById("sidebarSearchToggle");
  const input = document.getElementById("sidebarSearchInput");
  if (input) input.value = "";
  sidebarSearchQuery = "";
  document.getElementById("sidebarSearchStats")?.classList.remove("visible");
  if (wrapper) wrapper.style.display = "none";
  if (toggle) toggle.classList.remove("active");
  renderConversationList();
}

/* Legacy alias — in case any code calls clearSidebarSearch */
function clearSidebarSearch() { closeSidebarSearch(); }

// ── Init ──
async function initActiveTasks() {
  try {
    /* ── Parallel fetch: metadata + active tasks ── */
    /* Pass activeConvId (or restored conv from sessionStorage) to prefetch
       its messages in the same request, eliminating the second round-trip
       that shows "loading..." */
    const prefetchTarget = activeConvId || sessionStorage.getItem('chatui_activeConvId') || null;
    const [, , activeResp] = await Promise.all([
      loadConversationsFromServer(prefetchTarget),
      typeof loadFolders === 'function' ? loadFolders() : Promise.resolve(),
      fetch(apiUrl("/api/chat/active")),
    ]);
    /* ★ Migrate pinned conversations to a "⭐ 置顶" folder (one-time) */
    if (typeof _migratePinnedToFolder === 'function') _migratePinnedToFolder();
    if (!activeResp.ok) {
      _ensureNewest();
      return;
    }
    const serverTasks = await activeResp.json();
    // ★ Exclude aborted tasks — they are winding down and should not be reconnected to
    const runIds = new Set(
      serverTasks.filter((t) => t.status === "running" && !t.aborted).map((t) => t.id),
    );
    const toRecon = [];
    /* ★ Build a map from convId → running taskId for orphan recovery
          (handles the case where user refreshed before activeTaskId was saved) */
    const convIdToRunningTask = new Map();
    for (const t of serverTasks) {
      if (t.status === "running" && !t.aborted && t.convId) {
        // ★ Skip tasks that belong to branch streams — they are managed separately
        if (typeof isBranchTaskId === "function" && isBranchTaskId(t.id)) continue;
        convIdToRunningTask.set(t.convId, t.id);
      }
    }

    /* ── Batch-load messages only for convs that need task reconnection ── */
    const needMsgLoadIds = new Set();
    for (const conv of conversations) {
      if (conv._needsLoad) {
        if (conv.activeTaskId && runIds.has(conv.activeTaskId)) {
          needMsgLoadIds.add(conv.id);
        } else if (conv.activeTaskId) {
          needMsgLoadIds.add(conv.id);
        } else if (convIdToRunningTask.has(conv.id)) {
          needMsgLoadIds.add(conv.id);
        }
      }
    }
    if (needMsgLoadIds.size > 0) {
      await Promise.all(
        [...needMsgLoadIds].map((id) => loadConversationMessages(id)),
      );
    }

    /* ── Parallel poll for all finished tasks (Case B) ── */
    const caseBConvs = [];
    const caseEConvs = []; // Orphaned user messages (Case E)
    for (const conv of conversations) {
      /* Case A: conv has activeTaskId and that task is still running → reconnect */
      if (conv.activeTaskId && runIds.has(conv.activeTaskId)) {
        toRecon.push({ convId: conv.id, taskId: conv.activeTaskId });
        continue;
      }

      /* Case B: conv has activeTaskId but task finished/unknown → poll in batch */
      if (conv.activeTaskId) {
        caseBConvs.push(conv);
        continue;
      }

      /* Case C: ★ No activeTaskId, but server has a running task for this convId
            (user refreshed during "Preparing" before POST returned taskId)
            ★ Skip if the orphan is actually a branch task */
      const orphanTaskId = convIdToRunningTask.get(conv.id);
      if (orphanTaskId && !(typeof isBranchTaskId === "function" && isBranchTaskId(orphanTaskId))) {
        debugLog(
          `Recovering orphan task ${orphanTaskId.slice(0, 8)} for conv ${conv.id.slice(0, 8)}`,
          "warn",
        );
        const am = conv.messages[conv.messages.length - 1];
        /* Ensure there's an assistant message to stream into */
        if (!am || am.role !== "assistant") {
          conv.messages.push({
            role: "assistant",
            content: "",
            thinking: "",
            timestamp: Date.now(),
            toolRounds: [],
            model: conv.model || config.model || serverModel,
          });
        }
        conv.activeTaskId = orphanTaskId;
        toRecon.push({ convId: conv.id, taskId: orphanTaskId });
        continue;
      }

      /* Case D: No activeTaskId, no running server task — clean up ghost empty assistant messages
         (only for locally-loaded convs, not server-only shells) */
      if (!conv._needsLoad) {
        const lastMsg = conv.messages[conv.messages.length - 1];
        if (
          lastMsg &&
          lastMsg.role === "assistant" &&
          !lastMsg.content &&
          !lastMsg.thinking &&
          !lastMsg.error
        ) {
          console.warn(
            `[initActiveTasks CaseD] Removing ghost empty assistant message from conv ${conv.id.slice(0, 8)} ` +
            `(msgs=${conv.messages.length}, lastTimestamp=${lastMsg.timestamp ? new Date(lastMsg.timestamp).toISOString() : 'none'}). ` +
            `This could indicate a stream that started but never received any content.`,
          );
          conv.messages.pop();
          saveConversations(conv.id);
          syncConversationToServer(conv, { allowTruncate: true });
        }
      }

      /* Case E: ★ Orphaned user message — last msg is user, no activeTaskId,
         no running server task. This happens when:
         (a) sendMessage() was interrupted by page refresh during blocking translation wait
         (b) startAssistantResponse() failed silently and wasn't persisted
         (c) Network error prevented the POST /api/chat/start from completing
         Recovery: auto-start the assistant response so the user doesn't have to
         re-send. Only trigger for recent messages (< 5 min old) to avoid
         accidentally re-sending ancient stale messages.
         ★ SKIP image gen messages (🎨 prefix / _isImageGen) — those are handled
         by generateImageDirect(), NOT the orchestrator. Re-sending them to
         startAssistantResponse() would send them to the LLM, causing a freeze.

         ★ FIX: Also detect orphans in _needsLoad shell convs using metadata.
         Before this fix, shell convs (messages not loaded) silently skipped Case E
         because the guard `!conv._needsLoad && conv.messages.length > 0` excluded them.
         Now we check settings.lastMsgRole/lastMsgTimestamp from metadata. */
      {
        let _caseELastRole = null;
        let _caseELastTimestamp = null;
        let _caseESource = null;  // 'messages' or 'metadata'
        if (!conv._needsLoad && conv.messages.length > 0) {
          const lastMsg = conv.messages[conv.messages.length - 1];
          _caseELastRole = lastMsg?.role;
          _caseELastTimestamp = lastMsg?.timestamp;
          _caseESource = 'messages';
        } else if (conv._needsLoad && conv.lastMsgRole) {
          /* ★ Shell conv: use metadata persisted by syncConversationToServer.
           *   _applySettingsToConv maps settings.lastMsgRole → conv.lastMsgRole
           *   and settings.lastMsgTimestamp → conv.lastMsgTimestamp. */
          _caseELastRole = conv.lastMsgRole;
          _caseELastTimestamp = conv.lastMsgTimestamp;
          _caseESource = 'metadata';
        }
        if (_caseELastRole === 'user' && _caseELastTimestamp) {
          // ★ Skip image gen orphans — they belong to the creative mode pipeline
          // (can only check content for loaded convs; metadata orphans are assumed non-image-gen)
          let isImageGenOrphan = false;
          if (_caseESource === 'messages') {
            const lastMsg = conv.messages[conv.messages.length - 1];
            isImageGenOrphan = lastMsg._isImageGen || (lastMsg.content || '').startsWith('🎨 ');  // backward compat
          }
          if (isImageGenOrphan) {
            console.warn(
              `[initActiveTasks CaseE] ⏭ Skipping image gen orphan in conv ${conv.id.slice(0, 8)}`
            );
          } else {
            const ageMs = Date.now() - _caseELastTimestamp;
            const MAX_ORPHAN_AGE_MS = 5 * 60 * 1000; // 5 minutes
            if (ageMs < MAX_ORPHAN_AGE_MS) {
              console.warn(
                `[initActiveTasks CaseE] ★ Orphaned user message detected in conv ${conv.id.slice(0, 8)} ` +
                `(source=${_caseESource}, age=${(ageMs/1000).toFixed(0)}s). ` +
                `Auto-starting assistant response…`
              );
              // Defer to after the main recovery loop completes
              // so that all message loading and reconnections finish first
              caseEConvs.push(conv);
            }
          }
        }
      }
    }

    /* ── Case A: Reconnect to running tasks immediately ── */
    /* ★ CROSS-TALK DETECTION: warn when reconnecting multiple tasks simultaneously */
    if (toRecon.length > 1) {
      console.warn(
        `[initActiveTasks] ⚠️ MULTI-TASK RECONNECT: reconnecting ${toRecon.length} tasks simultaneously — ` +
        `elevated cross-talk risk! Tasks: ${toRecon.map(t => `conv=${t.convId.slice(0,8)}→task=${t.taskId.slice(0,8)}`).join(', ')} ` +
        `activeConvId=${activeConvId?.slice(0,8)||'null'}`
      );
    }
    for (const { convId, taskId } of toRecon) connectToTask(convId, taskId);
    // ── Reconnect any in-flight branch streams ──
    if (typeof initBranchReconnect === "function") initBranchReconnect();

    /* ── Render sidebar + active conv IMMEDIATELY ──
     *   Case B/F recovery runs in the background so the user sees their
     *   conversation without waiting for recovery of ALL conversations.
     *   With the backend's recover_stale_tasks_on_startup(), most Case B
     *   convs are already cleaned up (activeTaskId cleared, content merged). */
    renderConversationList();
    _ensureNewest();

    /* ── Background recovery: Case B + F + E (non-blocking) ── */
    const _bgRecovery = async () => {
    /* ── Case B: Batch-poll finished tasks in parallel ── */
    if (caseBConvs.length > 0) {
      console.warn(`[initActiveTasks] Case B: recovering ${caseBConvs.length} conversations with finished tasks`);
      await Promise.all(
        caseBConvs.map(async (conv) => {
          let am = conv.messages[conv.messages.length - 1];
          /* ★ Safety: if messages is still empty after loadConversationMessages
             (shouldn't happen after the core.js fix, but defensive), force-load */
          if (conv.messages.length === 0) {
            console.warn(`[initActiveTasks CaseB] conv=${conv.id.slice(0,8)} has 0 messages after load — force-recovering from server`);
            try {
              const recResp = await fetch(apiUrl(`/api/conversations/${conv.id}`));
              if (recResp.ok) {
                const recData = await recResp.json();
                if (recData.messages?.length > 0) {
                  conv.messages = recData.messages;
                  conv.title = recData.title || conv.title;
                  conv._serverMsgCount = conv.messages.length;
                  am = conv.messages[conv.messages.length - 1];
                  console.warn(`[initActiveTasks CaseB] ✅ Recovered ${conv.messages.length} messages from server`);
                }
              }
            } catch (recErr) {
              console.error(`[initActiveTasks CaseB] Recovery fetch failed:`, recErr);
            }
          }
          const localContentLen = am?.content?.length || 0;
          const localThinkingLen = am?.thinking?.length || 0;
          console.warn(`[initActiveTasks CaseB] conv=${conv.id.slice(0,8)} taskId=${conv.activeTaskId?.slice(0,8)} ` +
            `msgs=${conv.messages.length} localContent=${localContentLen}chars localThinking=${localThinkingLen}chars — polling server for task data...`);
          try {
            const pr = await fetch(
              apiUrl(`/api/chat/poll/${conv.activeTaskId}`),
            );
            if (pr.ok) {
              const td = await pr.json();
              const serverContentLen = td.content?.length || 0;
              const serverThinkingLen = td.thinking?.length || 0;
              console.warn(`[initActiveTasks CaseB] conv=${conv.id.slice(0,8)} server returned: ` +
                `content=${serverContentLen}chars thinking=${serverThinkingLen}chars error=${td.error||'none'} status=${td.status}`);
              
              /* ★ Endpoint mode: rebuild conv.messages from server's endpointTurns */
              if (td.endpointMode && td.endpointTurns && td.endpointTurns.length > 0) {
                let baseEnd = 0;
                for (let i = 0; i < conv.messages.length; i++) {
                  if (!conv.messages[i]._epIteration && !conv.messages[i]._isEndpointReview && !conv.messages[i]._isEndpointPlanner) {
                    baseEnd = i + 1;
                  }
                }
                const baseMsgs = conv.messages.slice(0, baseEnd);
                conv.messages = baseMsgs.concat(td.endpointTurns);
                am = conv.messages[conv.messages.length - 1];
                console.warn(`[initActiveTasks CaseB] ♾️ Endpoint mode — rebuilt messages: ` +
                  `base=${baseMsgs.length} epTurns=${td.endpointTurns.length} total=${conv.messages.length}`);
              }

              /* ★ BUG FIX: If local already has more content than server, KEEP local content
                 This prevents data loss when SSE accumulated content but task result was incomplete */
              if (am && am.role === "assistant") {
                if (td.content) {
                  if (localContentLen > serverContentLen) {
                    console.warn(`[initActiveTasks CaseB] ⚠️ KEEPING LOCAL content (${localContentLen} > server ${serverContentLen}) — would lose data!`);
                  } else {
                    am.content = td.content;
                  }
                }
                if (td.thinking) {
                  if (localThinkingLen > serverThinkingLen) {
                    console.warn(`[initActiveTasks CaseB] ⚠️ KEEPING LOCAL thinking (${localThinkingLen} > server ${serverThinkingLen}) — would lose data!`);
                  } else {
                    am.thinking = td.thinking;
                  }
                }
                if (td.error) am.error = td.error;
                if (td.toolRounds) am.toolRounds = td.toolRounds;
                if (td.finishReason) am.finishReason = td.finishReason;
                if (td.usage) am.usage = td.usage;
                if (td.preset) am.preset = td.preset;
                else if (td.effort) am.preset = td.effort;
                if (td.fallbackModel) am.fallbackModel = td.fallbackModel;
                if (td.fallbackFrom) am.fallbackFrom = td.fallbackFrom;
                if (td.modifiedFiles) am.modifiedFiles = td.modifiedFiles;
                /* ★ emit_to_user: recover emitContent from poll response */
                if (td.emitContent) am._emitContent = td.emitContent;
                if (td.emitToolName) am._emitToolName = td.emitToolName;
              }
              /* ★ If server returned status='interrupted', the task was checkpointed
                 but the server crashed before completing. Mark it as interrupted
                 so the user knows the response is partial. */
              if (td.status === 'interrupted' && am && am.role === 'assistant') {
                const recoveredLen = (am.content?.length || 0) + (am.thinking?.length || 0);
                if (recoveredLen > 0) {
                  if (!am.finishReason) am.finishReason = 'interrupted';
                  console.warn(`[initActiveTasks CaseB] ✅ Recovered ${recoveredLen} chars from server checkpoint (task was interrupted by server crash)`);
                } else {
                  am.error = "Task interrupted — server restarted before any content was generated.";
                }
              }
            } else if (pr.status === 404) {
              /* Task not found in memory or DB — check if the conversation's
                 messages already have content from a partial checkpoint sync.
                 (checkpoint_task_partial writes directly to conversation messages too) */
              const dbContentLen = am?.content?.length || 0;
              const dbThinkingLen = am?.thinking?.length || 0;
              console.warn(`[initActiveTasks CaseB] ⚠️ 404 for task ${conv.activeTaskId?.slice(0,8)} — task expired/cleaned up. ` +
                `Local content: ${dbContentLen}chars, thinking: ${dbThinkingLen}chars. ` +
                (dbContentLen > 0 || dbThinkingLen > 0 ? 'Preserving recovered data.' : 'No data — marking error.'));
              if (am && am.role === "assistant") {
                if (dbContentLen > 0 || dbThinkingLen > 0) {
                  am.finishReason = 'interrupted';
                } else {
                  am.error = "Task expired";
                }
              }
            }
          } catch (e) {
            console.error(`[initActiveTasks CaseB] Fetch error for conv=${conv.id.slice(0,8)}: ${e.message}`);
          }
          /* ★ FIX: Clean up orphaned awaiting_human / submitted HG rounds.
           *   Task is finished — any unanswered HG request is now dead. */
          let _hgCleaned = 0;
          let _timerCleaned = 0;
          for (const m of conv.messages) {
            if (m.toolRounds) {
              for (const r of m.toolRounds) {
                if (r.status === 'awaiting_human' || r.status === 'submitted') {
                  r.status = 'done';
                  r.guidanceId = null;
                  r._hgSkipped = true;
                  _hgCleaned++;
                }
                // ★ Clean up orphaned timer_create rounds — the task is dead,
                //   so the blocking poll can't complete. Mark as done and try
                //   to recover poll data from the API.
                if (r.toolName === 'timer_create' && r.status === 'searching') {
                  r.status = 'done';
                  r._timerOrphaned = true;
                  _timerCleaned++;
                  // Async: try to recover poll log from the timer API
                  if (r._timerTimerId && typeof _recoverTimerPolls === 'function') {
                    _recoverTimerPolls(r);
                  }
                }
              }
            }
          }
          if (_hgCleaned > 0) {
            console.info(`[initActiveTasks CaseB] 🧹 Cleaned ${_hgCleaned} orphaned HG round(s) — conv=${conv.id.slice(0,8)}`);
          }
          if (_timerCleaned > 0) {
            console.info(`[initActiveTasks CaseB] 🧹 Cleaned ${_timerCleaned} orphaned timer round(s) — conv=${conv.id.slice(0,8)}`);
          }
          conv.activeTaskId = null;
          conv._activeTaskClearedAt = Date.now();
          saveConversations(conv.id);
          syncConversationToServer(conv);
        }),
      );
    }

    /* ── Case F: Clear stale "server offline" errors now that server is back ── */
    /* When the frontend detected server offline (health check failure), it stamps
     * finishReason='server_offline' and error='Server offline — ...' on the last
     * assistant message and persists it.  On page refresh, this error text persists
     * even though the server is clearly back online (we just fetched /api/chat/active).
     *
     * Recovery: fetch the server's version of the conversation.  If the server has
     * a completed result (from _sync_result_to_conversation), adopt it.  Otherwise,
     * just clear the misleading error text — the "Server Offline" finish badge
     * already conveys the information without the alarming red error block. */
    {
      const offlineConvs = [];
      for (const conv of conversations) {
        if (conv._needsLoad) continue;
        const last = conv.messages[conv.messages.length - 1];
        if (last && last.role === 'assistant' && last.finishReason === 'server_offline') {
          offlineConvs.push(conv);
        }
      }
      if (offlineConvs.length > 0) {
        console.warn(`[initActiveTasks CaseF] ★ Clearing ${offlineConvs.length} stale "server_offline" error(s) — server is back online`);
        await Promise.all(offlineConvs.map(async (conv) => {
          const am = conv.messages[conv.messages.length - 1];
          const localContentLen = am.content?.length || 0;
          try {
            // Try to get server's version — it may have the completed result
            const resp = await fetch(apiUrl(`/api/conversations/${conv.id}`));
            if (resp.ok) {
              const data = await resp.json();
              const serverMsgs = data.messages || [];
              if (serverMsgs.length > 0) {
                const serverLast = serverMsgs[serverMsgs.length - 1];
                if (serverLast && serverLast.role === 'assistant') {
                  const serverContentLen = serverLast.content?.length || 0;
                  // If server has more content, adopt it (task completed after frontend gave up)
                  if (serverContentLen > localContentLen) {
                    console.warn(
                      `[initActiveTasks CaseF] conv=${conv.id.slice(0,8)}: server has MORE content ` +
                      `(${serverContentLen} > local ${localContentLen}) — adopting server version`
                    );
                    am.content = serverLast.content;
                    if (serverLast.thinking) am.thinking = serverLast.thinking;
                    if (serverLast.toolRounds) am.toolRounds = serverLast.toolRounds;
                    if (serverLast.finishReason && serverLast.finishReason !== 'server_offline') {
                      am.finishReason = serverLast.finishReason;
                    }
                    if (serverLast.usage) am.usage = serverLast.usage;
                    if (serverLast.model) am.model = serverLast.model;
                    if (serverLast.modifiedFiles) am.modifiedFiles = serverLast.modifiedFiles;
                    if (serverLast.modifiedFileList) am.modifiedFileList = serverLast.modifiedFileList;
                  }
                }
              }
            }
          } catch (e) {
            console.debug(`[initActiveTasks CaseF] Server fetch failed for conv=${conv.id.slice(0,8)}: ${e.message}`);
          }
          // Always clear the misleading error text — server is online now
          if (am.error && /server offline/i.test(am.error)) {
            console.info(
              `[initActiveTasks CaseF] conv=${conv.id.slice(0,8)}: clearing stale error text ` +
              `(content=${(am.content?.length||0)}chars, finishReason=${am.finishReason})`
            );
            delete am.error;
            saveConversations(conv.id);
            syncConversationToServer(conv);
          }
        }));
      }
    }

    /* ── Case E dispatch: auto-start responses for orphaned user messages ── */
    if (caseEConvs.length > 0) {
      console.warn(`[initActiveTasks CaseE] ★ Auto-starting ${caseEConvs.length} orphaned conversations`);
      /* ★ FIX: Delay Case E dispatch by 3s to give the user time to interact.
       *   Without delay, Case E fires startAssistantResponse immediately after
       *   page load, racing with the user's own sendMessage() if they quickly
       *   click a conversation and hit Send.  Both paths push assistant messages
       *   and POST /api/chat/start concurrently → broken SSE.
       *   The 3s delay lets the user's action take priority.  The re-check
       *   guard (activeTaskId / activeStreams) catches any user-initiated task. */
      setTimeout(() => {
        for (const conv of caseEConvs) {
          // Re-check: user may have already started a response for this conv
          if (conv.activeTaskId || activeStreams.has(conv.id)) {
            console.log(
              `[initActiveTasks CaseE] ⏭ Skipping conv ${conv.id.slice(0,8)} — ` +
              `task already started (activeTaskId=${conv.activeTaskId?.slice(0,8) || 'none'}, ` +
              `streaming=${activeStreams.has(conv.id)})`
            );
            continue;
          }
          debugLog(
            `Recovering orphaned message in "${conv.title?.slice(0,30)}…" — auto-starting assistant response`,
            "warn",
          );
          startAssistantResponse(conv.id);
        }
      }, 3000);
    }
    }; /* end _bgRecovery */

    /* Fire background recovery — don't await it */
    _bgRecovery().then(() => {
      /* Re-render after background recovery completes to show updated state */
      renderConversationList();
      if (activeConvId && !activeStreams.has(activeConvId)) {
        const c = getActiveConv();
        if (c && !c.activeTaskId) renderChat(c, false);
      }
    }).catch(e => console.warn('[initActiveTasks] Background recovery error:', e.message));

  } catch (e) {
    debugLog("initActiveTasks: " + e.message, "warn");
  }
}
function _ensureNewest() {
  if (_editingMsgIdx !== null) return;
  if (activeConvId) {
    if (activeStreams.has(activeConvId)) showStreamingUIForConv(activeConvId);
    else {
      const c = getActiveConv();
      if (c) renderChat(c);
    }
    // ★ Restore server-side queue state (survives page refresh)
    _refreshServerQueue(activeConvId);
  }
}

// ── Event bindings ──
(function init() {
  try {
  // ── Init model toggle from config ──
  (function initModelToggle() {
    thinkingEnabled = true;
    _applyModelUI(config.model || serverModel);
    _loadServerConfigAndPopulate();
  })();
  // fetchToggle / fetchBadge removed — fetch is always on
  document
    .getElementById("codeExecToggle")
    ?.classList.toggle("active", codeExecEnabled);
  document
    .getElementById("codeExecBadge")
    ?.classList.toggle("visible", codeExecEnabled);
  document
    .getElementById("browserToggle")
    ?.classList.toggle("active", browserEnabled);
  document
    .getElementById("browserBadge")
    ?.classList.toggle("visible", browserEnabled);
  document
    .getElementById("memoryToggle")
    ?.classList.toggle("active", memoryEnabled);
  document
    .getElementById("memoryBadge")
    ?.classList.toggle("visible", memoryEnabled);
  renderConversationList();
  function _handleConvClick(e) {
    const cpBtn = e.target.closest(".conv-copy-id");
    if (cpBtn) {
      e.stopPropagation();
      const cid = cpBtn.dataset.convId;
      if (cid) {
        _safeClipboardWrite(cid).then(() => {
          const orig = cpBtn.innerHTML;
          cpBtn.innerHTML = '✓';
          cpBtn.style.color = '#4ade80';
          setTimeout(() => { cpBtn.innerHTML = orig; cpBtn.style.color = ''; }, 1200);
        });
      }
      return;
    }
    // ★ Duplicate conversation button
    const dup = e.target.closest(".conv-dup");
    if (dup) {
      e.stopPropagation();
      if (dup.dataset.convId) duplicateConversation(dup.dataset.convId, e);
      return;
    }
    const del = e.target.closest(".conv-delete");
    if (del) {
      e.stopPropagation();
      if (del.dataset.convId) deleteConversation(del.dataset.convId, e);
      return;
    }
    /* pin button removed — pinning replaced by folders */
    // ★ @ reference button — add conversation reference chip
    const ref = e.target.closest(".conv-ref");
    if (ref) {
      e.stopPropagation();
      if (ref.dataset.convId) {
        addConvRef(ref.dataset.convId, ref.dataset.convTitle || "Untitled");
      }
      return;
    }
    // ★ Folder assign button — show folder picker dropdown
    const folderAssign = e.target.closest(".conv-folder-assign");
    if (folderAssign) {
      e.stopPropagation();
      if (folderAssign.dataset.convId) _showFolderPicker(folderAssign.dataset.convId, folderAssign);
      return;
    }
    // ★ Folder assign button — handled above
    const item = e.target.closest(".conv-item");
    if (item && item.dataset.convId) loadConversation(item.dataset.convId);
  }
  document
    .getElementById("convList")
    .addEventListener("click", _handleConvClick);
  // Initialize folder drag-and-drop
  _initFolderDragDrop();
  // ── Folder tab bar click + context menu ──
  _initFolderTabs();
  const ta = document.getElementById("userInput");
  ta.addEventListener("input", () => {
    ta.style.height = "auto";
    ta.style.height = Math.min(ta.scrollHeight, 200) + "px";
    if (_pendingLogClean && !ta.value.includes(_pendingLogClean.originalText))
      hideLogCleanBanner();
  });
  ta.addEventListener("paste", async (e) => {
    const items = e.clipboardData?.items;
    if (!items) return;
    let hasImage = false;
    for (const item of items) {
      if (item.type.startsWith("image/")) {
        e.preventDefault();
        hasImage = true;
        const f = item.getAsFile();
        const d = await processImageFile(f);
        pendingImages.push(d);
        renderImagePreviews();
        if (typeof _igUpdateGenButton === 'function') _igUpdateGenButton();
      }
    }
    // Detect log noise in pasted text
    if (!hasImage) {
      const pastedText = e.clipboardData?.getData("text");
      if (pastedText && pastedText.length > 200) {
        setTimeout(() => {
          const result = detectLogNoise(ta.value);
          if (result) showLogCleanBanner(result);
          else hideLogCleanBanner();
        }, 50);
      }
    }
  });
  // ── Full-page drag & drop (desktop only) ──
  // Disabled on mobile/touch devices: file uploads use the system file picker,
  // and drag events from sidebar conversation reordering falsely trigger the overlay.
  if (!("ontouchstart" in window)) {
    let _dragCounter = 0;
    const overlay = document.getElementById("dropOverlay");
    document.addEventListener("dragenter", (e) => {
      e.preventDefault();
      if (!e.dataTransfer || !e.dataTransfer.types.includes("Files")) return;
      _dragCounter++;
      if (_dragCounter === 1 && overlay) {
        overlay.classList.add("visible");
        // Update overlay text for paper mode
        const dropText = overlay.querySelector('.drop-text');
        const dropHint = overlay.querySelector('.drop-hint');
        if (typeof paperMode !== 'undefined' && paperMode) {
          if (dropText) dropText.textContent = 'Drop PDF to read';
          if (dropHint) dropHint.textContent = 'PDF files only in Paper Reading Mode';
        } else {
          if (dropText) dropText.textContent = 'Drop files here';
          if (dropHint) dropHint.textContent = 'Images · PDF · Word · Excel · PPT · Text files';
        }
      }
    });
    document.addEventListener("dragover", (e) => {
      if (e.dataTransfer && e.dataTransfer.types.includes("Files"))
        e.preventDefault();
    });
    document.addEventListener("dragleave", (e) => {
      e.preventDefault();
      if (!e.dataTransfer || !e.dataTransfer.types.includes("Files")) return;
      _dragCounter--;
      if (_dragCounter <= 0) {
        _dragCounter = 0;
        if (overlay) overlay.classList.remove("visible");
      }
    });
    document.addEventListener("drop", async (e) => {
      e.preventDefault();
      _dragCounter = 0;
      if (overlay) overlay.classList.remove("visible");
      const files = Array.from(e.dataTransfer?.files || []);

      // ★ Paper mode: route PDFs into the paper reader instead of main input
      if (typeof paperMode !== 'undefined' && paperMode) {
        for (const f of files) {
          if (f.type === "application/pdf" || f.name.toLowerCase().endsWith(".pdf")) {
            if (typeof _handlePaperFileDrop === 'function') {
              await _handlePaperFileDrop(f);
              break; // Only one PDF at a time in paper reader
            }
          }
        }
        return;
      }

      // ★ Edit mode uses the shared pendingImages/pendingPdfTexts — no separate handlers needed.
      // Dropped files go through the same path as the main input (below).
      for (const f of files) {
        if (f.type === "application/pdf" || f.name.toLowerCase().endsWith(".pdf"))
          await handlePDFUpload(f);
        else if (f.type.startsWith("image/"))
          await _handleImageDrop(f);
        else if (_DOC_EXTS.has(_getFileExt(f.name)))
          await handleDocUpload(f);
      }
    });
  }
  document.getElementById("settingsModal").addEventListener("click", (e) => {
    if (e.target === document.getElementById("settingsModal")) closeSettings();
  });
  /* Throttled scroll — updateActiveTurn is expensive (getBoundingClientRect on every turn dot) */
  let _scrollTicking = false;
  document.getElementById("chatContainer").addEventListener(
    "scroll",
    () => {
      if (!_scrollTicking) {
        _scrollTicking = true;
        requestAnimationFrame(() => {
          updateActiveTurn();
          _scrollTicking = false;
        });
      }
    },
    { passive: true },
  );
  document.addEventListener("keydown", (e) => {
    /* Ctrl/Cmd+K → toggle sidebar search */
    if ((e.ctrlKey || e.metaKey) && e.key === "k") {
      e.preventDefault();
      toggleSidebarSearch();
      return;
    }
    if (e.key === "Escape") {
      /* Close search panel if open */
      const sw = document.getElementById("sidebarSearchWrapper");
      if (sw && sw.style.display !== "none") {
        closeSidebarSearch();
        e.preventDefault();
        return;
      }
      const am = document.getElementById("applyModal");
      if (am && am.classList.contains("open")) {
        closeApplyModal();
        e.preventDefault();
        return;
      }
      const pm = document.getElementById("previewModal");
      if (pm && pm.classList.contains("open")) {
        closePreview();
        e.preventDefault();
        return;
      }
      const sm = document.getElementById("settingsModal");
      if (sm && sm.classList.contains("open")) {
        closeSettings();
        e.preventDefault();
        return;
      }
      const prm = document.getElementById("projectModal");
      if (prm && prm.classList.contains("open")) {
        closeProjectModal();
        e.preventDefault();
        return;
      }
      const brm = document.getElementById("browserModal");
      if (brm && brm.classList.contains("open")) {
        closeBrowserModal();
        e.preventDefault();
        return;
      }
      const cm = document.getElementById("dailyReportModal");
      if (cm && cm.classList.contains("open")) {
        closeDailyReport();
        e.preventDefault();
        return;
      }
    }
  });
  initSidebarSearch();
  /* ★ DB-first boot: conversations[] starts empty and is populated by
   *   loadConversationsFromServer() inside initActiveTasks().
   *   The sidebar shows a brief loading indicator (~16ms) until the
   *   server responds.  This eliminates all localStorage desync bugs. */
  /* ★ Restore last active conversation from sessionStorage (if any).
   *   If the conv exists on the server, we'll navigate to it after loading.
   *   Otherwise, fall back to the most recent conversation. */
  const _restoredConvId = sessionStorage.getItem('chatui_activeConvId') || null;
  newChat();  /* show welcome screen immediately */
  {
    const convList = document.getElementById('conversationList');
    if (convList) convList.innerHTML = '<div style="text-align:center;padding:18px 0;color:#999;font-size:13px">Loading…</div>';
  }
  // ── Startup DB health check — show persistent banner if PG is down ──
  _checkDbHealth();

  // ── Restore PDF/VLM state from sessionStorage (survives page refresh) ──
  if (typeof _vlmRestoreState === 'function') {
    _vlmRestoreState().catch(e => console.warn('[VLM-Restore] Failed:', e));
  }

  initActiveTasks().then(() => {
    renderConversationList();
    /* ★ Try to restore the last active conversation from before refresh */
    const restoredConv = _restoredConvId && conversations.find(c => c.id === _restoredConvId);
    if (restoredConv) {
      loadConversation(_restoredConvId);
    } else if (conversations.length > 0 && !conversations.find(c => c.id === activeConvId)) {
      /* Fall back: auto-select the most recent conversation */
      const input = document.getElementById('messageInput');
      const hasInput = input && input.value.trim().length > 0;
      if (!hasInput) {
        loadConversation(conversations[0].id);
      }
    }
    // After task reconnection, resume any pending translation tasks for active conv
    if (activeConvId) _resumePendingTranslations(activeConvId);
  }).catch(e => {
    debugLog(`Boot load failed: ${e.message}`, 'warn');
    /* Even if server load fails, the app is still usable — user can create new chats */
    renderConversationList();
  });
  if (typeof _initSelectionPopup === "function") _initSelectionPopup();
  loadPricing();
  loadProjectStatus();
  /* ★ Pre-fetch agent backend availability for the backend selector dropdown */
  _fetchAgentBackends().catch(() => {});
  _updateAutoApplyUI();
  _applyAutoTranslateUI();
  setInterval(() => {
    if (document.visibilityState === "visible" && _editingMsgIdx === null)
      loadConversationsFromServer();
  }, 60000);
  // ── Tab visibility: resume pending translations when user switches back ──
  document.addEventListener('visibilitychange', () => {
    if (document.visibilityState === 'visible' && activeConvId) {
      // Small delay to let the page settle after tab switch
      setTimeout(() => {
        if (activeConvId) {
          console.log(`%c[Translate] 👁 Tab visible — checking pending translations for conv=${activeConvId.slice(0,8)}`, 'color:#8b5cf6');
          _resumePendingTranslations(activeConvId);
        }
      }, 500);
    }
  });

  // ── Theme init ──
  applyTheme(_getCurrentTheme());

  // ── Toolbar layout: no overflow detection needed ──
  // CSS flex cascade (min-width:0 chain) handles truncation of .ps-label automatically.
  // .input-actions-scroll uses flex:0 1 auto to size-to-content without greedy fill.

  debugLog(
    `App initialized. tab=${TAB_ID} BASE_PATH="${BASE_PATH}"`,
    "success",
  );

  } catch (_initErr) {
    console.error('[main.js] ❌ Init crashed:', _initErr);
  }
  // Signal to loading-guard stubs that all scripts have loaded (MUST run even on error)
  if (typeof _markScriptsLoaded === 'function') _markScriptsLoaded();
})();
