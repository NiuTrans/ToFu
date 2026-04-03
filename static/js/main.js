/* ═══════════════════════════════════════════
   main.js — Chat Core, Toolbar, Init
   Orchestrator: sends messages, manages conversations,
   wires toolbar UI, and boots the app.
   Feature modules live in separate files:
     image-gen.js, log-clean.js, translation.js,
     upload.js, project.js, skills.js, scheduler.js, myday.js
   ═══════════════════════════════════════════ */

/* pendingPdfTexts → defined in upload.js */
/* _pendingLogClean → defined in log-clean.js */

/* ── Race-condition guard: incremented on every send or conversation switch ── */
let _sendGeneration = 0;

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
const _DEPTH_ICONS  = { off: '', medium: '', high: '', max: '' };
const _DEPTH_ICON_FALLBACK = '';
const _DEPTH_LABELS = { off: 'Off', medium: 'Med', high: 'High', max: 'Max' };
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
   *   - _scheduleReflow → _reflowToolbar (2–3 forced layout recalculations)
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
     * snapping instantly.  Worse, _reflowToolbar measures toolbar width via
     * rAF while the bar is mid-transition, getting incorrect measurements.
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

/* ★ _reflowToolbar: measure the toolbar's natural content width and size
 * .input-inner so the input box fits the content — no truncation, no empty gaps.
 * Called after every model/depth change or ig-mode toggle.
 *
 * Two independent CSS variables:
 *   --toolbar-w → .input-inner max-width (follows whichever toolbar is active)
 *   --chat-w    → .chat-inner max-width  (set only from .input-actions, never
 *                 changes when toggling ig-mode — prevents chat text reflow)
 *
 * Layout budget:
 *   toolbar natural width = sum of all flex children at their intrinsic sizes
 *   + .input-actions padding (12+8=20) + .input-box border (3)
 *   + safety margin (16)
 *
 * Clamped to [480, available_width] where available_width is the viewport
 * minus sidebar minus .input-area horizontal padding.
 */
function _reflowToolbar() {
  /* ★ BUG FIX: When image gen mode is active, .input-actions is display:none
   * and .ig-toolbar is the visible toolbar. Measuring .input-actions in that
   * state returns 0 scrollWidth → dialog shrinks to 480px minimum.
   * Now we measure whichever toolbar is actually visible. */
  const inputBox = document.querySelector('.input-box');
  const isIgMode = inputBox && inputBox.classList.contains('ig-active');
  const bar = isIgMode
    ? document.querySelector('.ig-toolbar')
    : document.querySelector('.input-actions');
  if (!bar) return;
  const root = document.documentElement;
  const inputInner = document.querySelector('.input-inner');
  /* Suppress transition during measurement to prevent visual flash */
  if (inputInner) inputInner.style.transition = 'none';
  /* Temporarily expand container so children aren't clamped during measurement.
   * Without this, scrollWidth of flex children with overflow:hidden returns
   * the current allocated width, not the content's natural width. */
  root.style.setProperty('--toolbar-w', '9999px');
  /* Force layout ONCE so flex children expand to their natural sizes.
   * All subsequent reads (scrollWidth, getComputedStyle) will be fast
   * because layout is already computed and hasn't been invalidated. */
  bar.offsetWidth; /* trigger single reflow */
  /* ★ PERF: Read gap from bar once — getComputedStyle after an offsetWidth
   * read is free (layout already computed, no style invalidation between). */
  const gap = parseFloat(getComputedStyle(bar).gap) || 6;
  /* Sum intrinsic widths of all direct children.
   * ★ PERF: Use offsetWidth (fast integer, layout already done) instead of
   * scrollWidth for children without overflow.  For the spacer check, use
   * a class-based test (.ig-flex) which avoids getComputedStyle entirely
   * for the common non-spacer children.  Only fall back to getComputedStyle
   * when a child is potentially hidden or a flex-grow spacer. */
  let natural = 0;
  let visibleCount = 0;
  const children = bar.children;
  for (let i = 0, len = children.length; i < len; i++) {
    const child = children[i];
    if (child.nodeType !== 1) continue;
    /* Fast path: skip display:none children.  offsetWidth === 0 is a proxy
     * for display:none (also true for zero-width children, but those add 0
     * to the sum anyway, so it's harmless). */
    const ow = child.offsetWidth;
    if (ow === 0) continue;
    visibleCount++;
    /* ★ BUG FIX: Skip flex-grow spacers (e.g. .ig-flex) during measurement.
     * When the container is temporarily 9999px wide, spacers with flex-grow > 0
     * expand to fill all remaining space, inflating scrollWidth to ~8500px+.
     * Use class-based detection first (no style lookup), fall back to
     * getComputedStyle only for ambiguous cases. */
    if (child.classList.contains('ig-flex') || (!child.textContent.trim() && parseFloat(getComputedStyle(child).flexGrow) > 0)) {
      natural += parseFloat(getComputedStyle(child).minWidth) || 0;
      continue;
    }
    natural += child.scrollWidth;
  }
  natural += Math.max(0, visibleCount - 1) * gap;
  /* Add padding + .input-box border + safety */
  const chrome = 20 + 3 + 16;
  let want = Math.ceil(natural + chrome);
  /* Clamp: min 480, max = available viewport width */
  const sidebar = document.getElementById('sidebar');
  const sidebarW = sidebar && !sidebar.classList.contains('collapsed') ? sidebar.offsetWidth : 0;
  const inputAreaPad = 48; /* 24px × 2 */
  const maxAvail = window.innerWidth - sidebarW - inputAreaPad;
  want = Math.max(480, Math.min(want, maxAvail));
  /* ★ DECOUPLED: --toolbar-w only controls .input-inner width.
   * --chat-w controls .chat-inner width and is set ONLY from the normal
   * toolbar (.input-actions), never from .ig-toolbar. This prevents
   * toggling image gen mode from reflowing the entire chat content area.
   *
   * When measuring .input-actions (normal mode), update BOTH variables.
   * When measuring .ig-toolbar (ig mode), only update --toolbar-w.
   * Edge case: if --chat-w was never set (page loaded straight into ig-mode),
   * fall back to the default (820px) — it will be set properly on the first
   * switch out of ig-mode or model change. */
  if (!isIgMode) {
    root.style.setProperty('--chat-w', want + 'px');
  }
  /* ★ BUG FIX: Set final width and flush layout BEFORE restoring transition.
   * The old code restored transition before the layout flush, so the browser
   * saw "max-width changed from 9999px → Xpx with .4s transition" → visible
   * expand-then-contract flash on every conv switch / model change.
   *
   * Correct order:
   *   1. Write final --toolbar-w (transition still 'none')
   *   2. Force reflow → browser commits Xpx as the computed value
   *   3. THEN restore transition → browser sees no pending change to animate */
  root.style.setProperty('--toolbar-w', want + 'px');
  if (inputInner) inputInner.offsetWidth; /* flush with transition:none */
  /* Now safe to restore transition — computed max-width is already final */
  if (inputInner) inputInner.style.transition = '';
}

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
function _applySkillsUI(enabled) {
  skillsEnabled = !!enabled;
  document
    .getElementById("skillsToggle")
    ?.classList.toggle("active", skillsEnabled);
  document
    .getElementById("skillsBadge")
    ?.classList.toggle("visible", skillsEnabled);
  _updateSkillsModalBtn();
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
    ? '描述你想生成的图片 / 粘贴图片后描述修改内容…'
    : 'Type your message...';
  const hint = document.getElementById('inputHint');
  if (hint) hint.textContent = imageGenMode
    ? 'Ctrl+Enter 生成 · Esc 退出 · 粘贴/拖拽图片可编辑 · 支持中英文'
    : 'Ctrl+Enter send · Drop files anywhere';
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
  conv.skillsEnabled = !!skillsEnabled;
  conv.schedulerEnabled = !!schedulerEnabled;
  conv.swarmEnabled = !!swarmEnabled;
  conv.endpointEnabled = !!endpointEnabled;
  conv.imageGenEnabled = !!imageGenEnabled;
  conv.imageGenMode = !!imageGenMode;
  conv.humanGuidanceEnabled = !!humanGuidanceEnabled;
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
  saveConversations(conv.id);
  /* ★ BUG FIX: Do NOT sync empty convs to server.
   * When a new conv is created, _saveConvToolState() fires before the user
   * message is pushed.  Syncing messages:[] overwrites the server with nothing,
   * which causes the conv to flicker/disappear on reload (_serverMsgCount=0,
   * _purgeEmptyConvs kills it).  Only sync when there are actual messages. */
  if (conv.messages && conv.messages.length > 0) {
    syncConversationToServerDebounced(conv);
  } else {
    console.log(`[_saveConvToolState] Skipped server sync — conv ${conv.id.slice(0,8)} has no messages yet`);
  }
}
function _restoreConvToolState(conv) {
  config.thinkingDepth = conv.thinkingDepth || null;   // ← restore depth BEFORE model UI (let _applyModelUI normalize)
  _applyModelUI(conv.model || conv.preset || conv.effort || serverModel);
  _applySearchModeUI(conv.searchMode || "multi");
  _applyFetchEnabledUI(true);  // always on
  _applyCodeExecUI(!!conv.codeExecEnabled);
  _applyBrowserUI(!!conv.browserEnabled);
  _applyDesktopUI(!!conv.desktopEnabled);
  _applySkillsUI(conv.skillsEnabled !== undefined ? !!conv.skillsEnabled : true);
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
  if (typeof updateSubmenuCounts === 'function') updateSubmenuCounts();
}
function _resetToolsToDefaults() {
  config.thinkingDepth = config.defaultThinkingDepth;   // ← reset to default depth BEFORE applying model UI (let _applyModelUI normalize)
  _applyModelUI(serverModel);
  _applySearchModeUI("multi");
  _applyFetchEnabledUI(true);
  _applyCodeExecUI(false);
  _applyBrowserUI(false);
  _applySkillsUI(true);
  _applySwarmUI(false);
  _applyEndpointUI(false);
  _applyImageGenToolUI(false);
  _applyImageGenUI(false);
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
  if (genText) genText.textContent = '生成';
  if (typeof updateSubmenuCounts === 'function') updateSubmenuCounts();
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
    prevConv.skillsEnabled = !!skillsEnabled;
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
  document.getElementById("topbarTitle").textContent = "New Chat";
  renderConversationList();
  if (typeof clearDebug === "function") clearDebug();
  document.getElementById("chatInner").innerHTML =
    `<div class="welcome" id="welcome"><div class="welcome-icon"><img src="${BASE_PATH}/static/icons/tofu-welcome.svg" alt="Tofu" width="64" height="64"></div><h2 class="tofu-brand"><span class="tofu-brand-t">T</span><span class="tofu-brand-o1">o</span><span class="tofu-brand-f">f</span><span class="tofu-brand-u">u</span><small>豆腐</small></h2><p>看似简单，实则百搭 — search, code, browse, trade, and more.</p><div class="feature-pills"><span class="feature-pill">Extended Thinking</span><span class="feature-pill">Search</span><span class="feature-pill">URL Fetch</span><span class="feature-pill">Image Input</span><span class="feature-pill">Co-Pilot</span><span class="feature-pill">Browser</span></div></div>`;
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
    prevConv.skillsEnabled = !!skillsEnabled;
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
      /* Re-check: don't double-sync if _saveConvToolState was called elsewhere */
      saveConversations(pc.id);
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
// ═══════════════════════════════════════════════════════
// ★ Format a conversation's messages into plain text for LLM context
// ═══════════════════════════════════════════════════════
function _formatConvRefText(title, msgs) {
  if (!msgs || msgs.length === 0) return "(Empty conversation)";
  const lines = [];
  for (let i = 0; i < msgs.length; i++) {
    const m = msgs[i];
    const num = i + 1;
    if (m.role === "user") {
      lines.push(`── Message ${num} [User] ──`);
      lines.push(m.content || "(empty)");
    } else if (m.role === "assistant") {
      lines.push(`── Message ${num} [Assistant] ──`);
      // Include thinking/reasoning if present
      if (m.thinking) lines.push(`<thinking>\n${m.thinking}\n</thinking>`);
      lines.push(m.content || "(empty)");
      // Include tool calls if present
      if (m.toolCalls && m.toolCalls.length > 0) {
        for (const tc of m.toolCalls) {
          lines.push(`  Tool Call: ${tc.function?.name || tc.name || "unknown"}`);
          const args = tc.function?.arguments || tc.arguments;
          if (args) {
            const argStr = typeof args === "string" ? args : JSON.stringify(args, null, 2);
            // Truncate very large tool arguments
            lines.push(`     Arguments: ${argStr.length > 2000 ? argStr.slice(0, 2000) + "\n     ... (truncated)" : argStr}`);
          }
        }
      }
    } else if (m.role === "tool") {
      lines.push(`── Message ${num} [Tool Result: ${m.name || "unknown"}] ──`);
      const content = m.content || "";
      // Truncate very large tool results
      lines.push(content.length > 3000 ? content.slice(0, 3000) + "\n... (truncated)" : content);
    } else if (m.role === "system") {
      lines.push(`── Message ${num} [System] ──`);
      lines.push(m.content || "");
    }
    lines.push(""); // blank separator
  }
  return lines.join("\n");
}

// ★ FIX: buildApiMessages — include tool usage history
// ══════════════════════════════════════════════════════
function buildApiMessages(conv, opts = {}) {
  const messages = [];
  if (config.systemPrompt?.trim())
    messages.push({ role: "system", content: config.systemPrompt.trim() });
  const srcMsgs = opts.includeAll ? conv.messages : conv.messages.slice(0, -1);
  for (const msg of srcMsgs) {
    if (msg.role === "user") {
      const hasImages =
        msg.images?.length > 0 && msg.images.some((img) => img.base64 || img.url);
      const hasPdfTexts = msg.pdfTexts?.length > 0;
      let textContent = msg.content || "";
      // ★ Strip <notranslate>/<nt> wrapper tags but keep inner content — chat LLM sees the full text
      if (textContent.includes('<notranslate>') || textContent.includes('<nt>')) {
        textContent = textContent.replace(/<\/?notranslate>/gi, '').replace(/<\/?nt>/gi, '');
      }
      // ★ Prepend reply quotes if present (supports array and legacy single)
      const quotes = msg.replyQuotes || (msg.replyQuote ? [msg.replyQuote] : []);
      if (quotes.length > 0) {
        const quotesBlock = quotes.map((q, i) => `[引用${quotes.length > 1 ? (i+1) : ""}]\n${q}\n[/引用${quotes.length > 1 ? (i+1) : ""}]`).join("\n\n");
        textContent = `${quotesBlock}\n\n${textContent}`;
      }
      // ★ Prepend conversation references if present
      if (msg.convRefTexts && msg.convRefTexts.length > 0) {
        const refsBlock = msg.convRefTexts.map((cr, i) =>
          `[REFERENCED_CONVERSATION${msg.convRefTexts.length > 1 ? ` #${i+1}` : ""} title="${cr.title}" id="${cr.id}"]\n${cr.text}\n[/REFERENCED_CONVERSATION]`
        ).join("\n\n");
        textContent = `The user has attached the following conversation(s) for reference:\n\n${refsBlock}\n\n---\n\n${textContent}`;
      }

      if (hasPdfTexts) {
        for (const pdf of msg.pdfTexts) {
          textContent += `\n\n${"═".repeat(50)}\nPDF Document: ${pdf.name} (${pdf.pages} pages, ${(pdf.textLength / 1024).toFixed(1)}KB)\n${"═".repeat(50)}\n${pdf.text}`;
        }
      }

      if (hasImages) {
        const content = [];
        msg.images.forEach((img) => {
          // Prefer base64 data URL; pass through server URL as fallback
          // (backend _validate_image_blocks resolves /api/images/ from disk)
          let imgUrl = "";
          if (img.base64) {
            imgUrl = `data:${img.mediaType};base64,${img.base64}`;
          } else if (img.url) {
            // base64 not hydrated (page reload race, proxy error, etc.)
            // Pass through — backend will resolve local /api/images/ URLs from disk
            imgUrl = img.url;
            console.warn(`[buildApiMessages] Image base64 not hydrated, passing URL: ${img.url.slice(0, 80)}`);
          }
          if (imgUrl) {
            content.push({
              type: "image_url",
              image_url: { url: imgUrl },
            });
            if (img.caption)
              content.push({
                type: "text",
                text: `[PDF p${img.pdfPage || "?"}: ${img.caption}]`,
              });
            else if (img.pdfPage)
              content.push({
                type: "text",
                text: `[PDF page ${img.pdfPage}/${img.pdfTotal || "?"}]`,
              });
          }
        });
        if (textContent) content.push({ type: "text", text: textContent });
        messages.push({ role: "user", content });
      } else {
        messages.push({ role: "user", content: textContent });
      }
    } else if (msg.role === "assistant") {
      // ★ Build tool usage summary (used as fallback content if assistant text is empty)
      let toolCtx = "";
      if (msg.toolSummary) {
        toolCtx = msg.toolSummary;
      } else {
        // Fallback: serialize raw tool calls (for old messages without toolSummary)
        const rounds = getSearchRoundsFromMsg(msg);
        if (rounds.length > 0) {
          const calls = rounds.map(r => {
            const call = { name: r.toolName || "unknown" };
            if (r.toolArgs) Object.assign(call, r.toolArgs);
            else if (r.query) call.query = r.query;
            return call;
          });
          toolCtx = JSON.stringify(calls);
        }
      }

      // ★ FIX: Never skip assistant messages — empty content breaks
      //   user↔assistant alternation causing consecutive USER messages.
      //   If content is empty, use tool summary as placeholder content.
      const assistantContent = msg.content || toolCtx;
      messages.push({ role: "assistant", content: assistantContent });


    }
  }
  return messages;
}

async function startAssistantResponse(convId) {
  const conv = conversations.find((c) => c.id === convId);
  if (!conv || activeStreams.has(convId) || conv.activeTaskId) return;
  /* ★ Ensure messages are loaded — Case E recovery can call this for _needsLoad shell convs
   *   that were detected as orphans via metadata (lastMsgRole/lastMsgTimestamp).
   *   Without loading first, conv.messages is empty → buildApiMessages has no context. */
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
    searchRounds: [],
    model: _convModel,
  };
  conv.messages.push(assistantMsg);
  if (activeConvId === convId) {
    const inner = document.getElementById("chatInner");
    if (inner) {
      const el = document.createElement("div");
      el.className = "message message-new";
      el.addEventListener('animationend', () => el.classList.remove('message-new'), { once: true });
      el.id = "streaming-msg";
      /* ★ Endpoint mode: show Planner avatar/role from the start.
       * The endpoint_iteration(planning) event may fire before SSE connects,
       * so the initial bubble must already show "Planner". */
      const _isEndpoint = (convId === activeConvId) ? endpointEnabled : (!!conv.endpointEnabled);
      const _streamAvatar = _isEndpoint
        ? ((typeof _TOFU_PLANNER_SVG !== 'undefined') ? _TOFU_PLANNER_SVG : '✦')
        : ((typeof _TOFU_WORKER_SVG !== 'undefined') ? _TOFU_WORKER_SVG : '✦');
      const _streamRole = _isEndpoint ? 'Planner' : 'Agent';
      const _streamStatus = _isEndpoint ? 'Planning…' : 'Preparing...';
      const _streamClass = _isEndpoint ? 'ep-planner-msg' : 'ep-worker-msg';
      if (_isEndpoint) assistantMsg._isEndpointPlanner = true;
      el.innerHTML = `<div class="message-avatar">${_streamAvatar}</div><div class="message-content"><div class="message-header"><span class="message-role">${_streamRole}</span><span class="message-time">${new Date().toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" })}</span><span id="stream-elapsed-timer" class="stream-elapsed-timer"></span></div><div class="message-body" id="streaming-body"><div class="stream-status"><div class="pulse"></div> ${_streamStatus}</div></div></div>`;
      if (_streamClass) el.classList.add(_streamClass);
      inner.appendChild(el);
      scrollToBottom();
    }
  }
  /* ★ Don't persist the empty assistant message yet — wait until we have a taskId.
        This prevents a "ghost" empty message if the user refreshes before POST returns. */
  buildTurnNav(conv);
  /* ★ Wait for image hydration to complete — after page reload, images loaded
     from DB only have url (no base64). _hydrateImageBase64 fetches them in
     background, but buildApiMessages needs base64 for the LLM API. */
  if (conv._hydratePromise) await conv._hydratePromise;
  const apiMessages = buildApiMessages(conv);
  // ★ Show full messages in debug panel for inspection
  showMessagesInDebug(
    apiMessages,
    `${conv.messages.length}条对话`,
    false,
    convId,
  );
  let taskId;
  /* ── ★ FIX: Read tool config from per-conversation state, NOT globals ──
   * When startAssistantResponse is called for a BACKGROUND conversation
   * (e.g. from _dispatchQueuedMessage after finishStream), the global
   * variables (searchMode, browserEnabled, projectState, etc.) reflect
   * the CURRENTLY VIEWED conversation, not convId's conversation.
   * This caused crosstalk: Chat B's project/browser settings leaked into Chat A.
   * Solution: always read from conv.* which _saveConvToolState() keeps in sync. */
  const _isActive = (convId === activeConvId);
  const _sm   = _isActive ? searchMode           : (conv.searchMode || "multi");
  const _fe   = _isActive ? fetchEnabled          : (!!conv.fetchEnabled);
  const _ce   = _isActive ? codeExecEnabled       : (!!conv.codeExecEnabled);
  const _sk   = _isActive ? skillsEnabled         : (conv.skillsEnabled !== undefined ? !!conv.skillsEnabled : true);
  const _sch  = _isActive ? schedulerEnabled      : (!!conv.schedulerEnabled);
  const _sw   = _isActive ? swarmEnabled           : (!!conv.swarmEnabled);
  const _be   = _isActive ? browserEnabled         : (!!conv.browserEnabled);
  const _de   = _isActive ? desktopEnabled         : (!!conv.desktopEnabled);
  const _ep   = _isActive ? endpointEnabled        : (!!conv.endpointEnabled);
  const _ig   = _isActive ? imageGenEnabled        : (!!conv.imageGenEnabled);
  const _hg   = _isActive ? humanGuidanceEnabled   : (!!conv.humanGuidanceEnabled);
  const _pre  = _isActive ? (config.model || serverModel)     : (conv.model || serverModel);
  const _dep  = _isActive ? config.thinkingDepth : (conv.thinkingDepth || null);
  /* ★ FIX: ALWAYS read project path from per-conv state (conv.projectPath),
   * never from the global projectState singleton.  The global state can be
   * stale when: (a) user switches convs with text in the input box
   * (skipping _restoreConvProject), (b) _restoreConvProject is async and
   * hasn't completed yet, (c) another conv's _restoreConvProject changed
   * the global.  conv.projectPath is always up-to-date because
   * _saveConvProjectPath() updates it synchronously on every UI change. */
  const _pp   = _getConvProjectPath(conv);
  /* Decide API route: endpoint mode uses /api/endpoint/start */
  const startUrl = _ep
    ? apiUrl("/api/endpoint/start")
    : apiUrl("/api/chat/start");
  try {
    const baseConfig = {
      maxTokens: config.maxTokens,
      model: serverModel,
      thinkingEnabled,
      preset: _pre,
      model: _pre,
      thinkingDepth: _dep,
      temperature: config.temperature,
      searchMode: _sm,
      fetchEnabled: _fe,
      codeExecEnabled: _ce,
      skillsEnabled: _sk,
      schedulerEnabled: _sch,
      swarmEnabled: _sw,
      projectPath: _pp,
      autoApply: autoApplyWrites,
      browserEnabled: _be,
      desktopEnabled: _de,
      imageGenEnabled: _ig,
      humanGuidanceEnabled: _hg,
      /* ★ AutoTranslate flag: tell the backend whether autoTranslate is on
       * so it can skip the _needs_translation heuristic and always translate. */
      autoTranslate: conv.autoTranslate !== undefined ? !!conv.autoTranslate : !!autoTranslate,
      /* ★ Per-client browser routing: send the client ID of the extension
       * that should execute browser commands for this task. */
      browserClientId: _be ? (window._browserClientId || null) : null,
    };
    /* Endpoint-specific config — critic uses same model+tools as worker */
    if (_ep) {
      baseConfig.endpointMode = true;
    }
    const resp = await fetch(startUrl, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        convId,
        messages: apiMessages,
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
      `Task started: ${taskId} (${_ep ? "ENDPOINT" : "normal"} search:${_sm} fetch:${_fe} project:${_pp ? "yes" : "no"} browser:${_be} autoApply:${autoApplyWrites}${_isActive ? "" : " bg-conv"})`,
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
    conversations.unshift(conv);
    activeConvId = conv.id;
    sessionStorage.setItem('chatui_activeConvId', conv.id);
    _saveConvToolState();
    renderConversationList();
  }
  // ── ★ Queue-on-stream: if currently streaming OR translating, queue message for later dispatch ──
  if (activeStreams.has(conv.id) || conv.activeTaskId || conv._translating) {
    const queuedItem = {
      text: finalText,
      images: [...pendingImages],
      pdfTexts: [...pendingPdfTexts],
      timestamp: Date.now(),
    };
    // Capture reply quotes
    if (typeof getPendingReplyQuotes === "function") {
      const rqs = getPendingReplyQuotes();
      if (rqs && rqs.length > 0) {
        queuedItem.replyQuotes = rqs;
        clearReplyQuote();
      }
    }
    // Capture conv refs (just the refs, not the fetched text — we'll fetch at send time)
    if (typeof getPendingConvRefs === "function") {
      const crs = getPendingConvRefs();
      if (crs && crs.length > 0) {
        queuedItem.convRefs = crs;
        clearConvRefs();
      }
    }
    if (!pendingMessageQueue.has(conv.id)) pendingMessageQueue.set(conv.id, []);
    pendingMessageQueue.get(conv.id).push(queuedItem);
    const depth = pendingMessageQueue.get(conv.id).length;
    const hasImg = (queuedItem.images?.length || 0) > 0;
    const hasPdf = (queuedItem.pdfTexts?.length || 0) > 0;
    const hasRef = (queuedItem.convRefs?.length || 0) > 0;
    const hasQuote = (queuedItem.replyQuotes?.length || 0) > 0;
    const attachInfo = [hasImg && `${queuedItem.images.length}img`, hasPdf && `${queuedItem.pdfTexts.length}pdf`, hasRef && `${queuedItem.convRefs.length}ref`, hasQuote && `${queuedItem.replyQuotes.length}quote`].filter(Boolean).join('+');
    console.log(
      `%c[Queue] ✚ Enqueued %c#${depth}%c for conv=${conv.id.slice(0,8)} | text=${finalText.length}ch${attachInfo ? ' | attach=' + attachInfo : ''} | reason=${activeStreams.has(conv.id) ? 'streaming' : conv._translating ? 'translating' : 'taskActive'}`,
      'color:#a78bfa;font-weight:bold', 'color:#fbbf24;font-weight:bold', 'color:#a78bfa'
    );
    // Clear input immediately so user sees it was accepted
    input.value = "";
    input.style.height = "auto";
    pendingImages = [];
    pendingPdfTexts = [];
    renderImagePreviews();
    document.getElementById("pdfProgress").style.display = "none";
    renderPendingQueueUI(conv.id);
    updateSendButton();
    debugLog(`消息已排队 (#${depth})，将在当前回复结束后自动发送`, 'info');
    return;
  }
  const convId = conv.id;
  const userMsg = {
    role: "user",
    content: finalText,
    images: [...pendingImages],
    pdfTexts: [...pendingPdfTexts],
    timestamp: Date.now(),
  };
  // ── Reply quotes: attach if pending (supports multiple) ──
  if (typeof getPendingReplyQuotes === "function") {
    const rqs = getPendingReplyQuotes();
    if (rqs && rqs.length > 0) {
      userMsg.replyQuotes = rqs;
      clearReplyQuote();
    }
  }
  // ── Conversation references: attach if pending ──
  if (typeof getPendingConvRefs === "function") {
    const crs = getPendingConvRefs();
    if (crs && crs.length > 0) {
      userMsg.convRefs = crs;
      clearConvRefs();
      // Fetch and format all referenced conversations client-side
      const fetchPromises = crs.map(async (cr) => {
        try {
          // First try to use locally cached conversation data
          let convMsgs = null;
          const localConv = conversations.find(c => c.id === cr.id);
          if (localConv && localConv.messages && localConv.messages.length > 0) {
            convMsgs = localConv.messages;
          } else {
            // Fall back to fetching from the server (existing endpoint)
            const resp = await fetch(apiUrl(`/api/conversations/${cr.id}`));
            if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
            const data = await resp.json();
            convMsgs = data.messages || [];
          }
          return { id: cr.id, title: cr.title, text: _formatConvRefText(cr.title, convMsgs) };
        } catch (e) {
          return { id: cr.id, title: cr.title, text: `[Error loading conversation: ${e.message}]` };
        }
      });
      userMsg.convRefTexts = await Promise.all(fetchPromises);
    }
  }
  // ── Auto-translate: detect Chinese (defer actual translation until after render) ──
  // ★ Use per-conv state (just saved by _saveConvToolState above) for consistency
  const _sendAutoTranslate = conv.autoTranslate !== undefined ? !!conv.autoTranslate : !!autoTranslate;
  let needsTranslation = false;
  if (_sendAutoTranslate && finalText) {
    const hasChinese = /[\u4e00-\u9fff\u3400-\u4dbf]/.test(finalText);
    if (hasChinese) {
      userMsg.originalContent = finalText;
      needsTranslation = true;
    }
  }
  // ── Immediately push message & render the chat page (no blocking) ──
  /* ★ FIX: Clear _needsLoad BEFORE pushing the user message.
   * If the server was down when sendMessage() called loadConversationMessages(),
   * _needsLoad stays true.  After server restart, loadConversationsFromServer()
   * sees _needsLoad=true and re-fetches from the DB, OVERWRITING the local
   * user message with stale server data → permanent message loss.
   * By clearing _needsLoad here, we declare: "this conv has local mutations
   * that are the source of truth — don't overwrite from server." */
  conv._needsLoad = false;
  conv.messages.push(userMsg);
  const userMsgIdx = conv.messages.length - 1;
  if (
    conv.messages.filter((m) => m.role === "user").length === 1 &&
    finalText
  ) {
    // ★ Strip <notranslate>/<nt> wrapper tags so they don't appear in sidebar titles
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
  // ── Auto-translate user message: NON-BLOCKING background translation ──
  // ★ Fire-and-forget: translation runs in background, UI is freed immediately.
  //   Sidebar shows "翻译中" status. When translation finishes, auto-starts assistant.
  if (needsTranslation) {
    conv._translating = true;
    conv._translateAborted = false;
    updateSendButton();
    renderConversationList();
    // ★ Show subtle inline indicator on the user message itself
    if (activeConvId === convId) {
      const msgEl = document.getElementById('msg-' + userMsgIdx);
      if (msgEl) msgEl.classList.add('user-translating');
    }
    // ★ NON-BLOCKING: fire background translation, then auto-start assistant
    _translateThenRespond(conv, convId, userMsg, userMsgIdx, finalText);
    // sendMessage returns immediately — input is free for next interaction
    return;
  }
  // ── ★ Wait for VLM parsing to complete (mandatory for PDFs) ──
  // VLM runs in parallel with translation, so by now it may already be done.
  // If not, block here until it finishes (or fails/times out).
  await _waitForVlmParsing(userMsg, convId, userMsgIdx);

  saveConversations(convId);
  /* ★ Proceed to start assistant response — translation is already done (or failed/timed out).
     Race guard: if user switched conv, still save & start the response
     but don't let startAssistantResponse render into wrong conv's DOM */
  await startAssistantResponse(convId);
}

// ══════════════════════════════════════════════════════
//  ★ Non-blocking background translation → auto-start assistant
// ══════════════════════════════════════════════════════

/**
 * Translate user message in background, then auto-start assistant response.
 * Called fire-and-forget from sendMessage / saveEditAndResend / regenerateFromUser
 * so the UI stays responsive during translation.
 *
 * State transitions visible in sidebar:
 *   "翻译中" (conv._translating=true) → streaming dot (assistant running) → done
 */
async function _translateThenRespond(conv, convId, userMsg, userMsgIdx, originalText) {
  const _translateCtrl = new AbortController();
  conv._translateAbortCtrl = _translateCtrl;
  let _translateWasAborted = false;
  try {
    // Run translation + DB sync in parallel
    const _translatePromise = fetch(apiUrl('/api/translate'), {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      signal: _translateCtrl.signal,
      body: JSON.stringify({
        text: originalText,
        targetLang: 'English',
        sourceLang: 'Chinese',
      }),
    });
    const [translateResp] = await Promise.all([
      _translatePromise,
      syncConversationToServer(conv),
    ]);
    if (conv._translateAborted) {
      _translateWasAborted = true;
    } else if (translateResp.ok) {
      const d = await translateResp.json();
      if (d.translated) {
        userMsg.content = d.translated;
        userMsg._translateDone = true;
        saveConversations(convId);
        syncConversationToServer(conversations.find(c => c.id === convId));
        if (activeConvId === convId) {
          const msgEl = document.getElementById('msg-' + userMsgIdx);
          if (msgEl) { msgEl.classList.remove('user-translating'); msgEl.outerHTML = renderMessage(userMsg, userMsgIdx); }
        }
        console.log('%c[Translate] ✓ Background translation done', 'color:#22c55e;font-weight:bold');
      } else {
        console.warn('[Translate] No translated text in response, proceeding with original');
      }
    } else if (translateResp.status === 413) {
      // Text too long for sync — fall back to async task
      console.warn('[Translate] Text too long for sync, falling back to async');
      const taskId = await _startTranslateTask(originalText, 'English', 'Chinese', convId, userMsgIdx, 'content');
      if (taskId) {
        for (let attempt = 0; attempt < 40; attempt++) {
          await new Promise(r => setTimeout(r, attempt < 3 ? 1000 : 1500));
          if (conv._translateAborted) { _translateWasAborted = true; break; }
          const result = await _pollTranslateTask(taskId);
          if (result.status === 'done' && result.translated) {
            userMsg.content = result.translated;
            userMsg._translateDone = true;
            saveConversations(convId);
            syncConversationToServer(conversations.find(c => c.id === convId));
            if (activeConvId === convId) {
              const msgEl = document.getElementById('msg-' + userMsgIdx);
              if (msgEl) { msgEl.classList.remove('user-translating'); msgEl.outerHTML = renderMessage(userMsg, userMsgIdx); }
            }
            break;
          } else if (result.status === 'error' || result.status === 'not_found') { break; }
        }
      }
    } else {
      console.warn('[Translate] Background translate HTTP', translateResp.status, '— proceeding with original');
    }
  } catch (e) {
    if (e.name === 'AbortError') {
      console.log('%c[Translate] ✗ Translation aborted by user', 'color:#f59e0b;font-weight:bold');
      _translateWasAborted = true;
    } else {
      console.error('Translation failed, proceeding with original text:', e);
    }
  }
  // Clean up translating state
  conv._translating = false;
  conv._translateAborted = false;
  conv._translateAbortCtrl = null;
  updateSendButton();
  renderConversationList();
  if (activeConvId === convId) {
    const msgEl = document.getElementById('msg-' + userMsgIdx);
    if (msgEl) msgEl.classList.remove('user-translating');
  }
  if (_translateWasAborted) {
    console.log('%c[Translate] Skipping LLM call — translation was aborted', 'color:#f59e0b;font-weight:bold');
    saveConversations(convId);
    if (activeConvId === convId) renderChat(conv);
    return;
  }
  // ── Translation done → proceed to VLM wait + assistant response ──
  await _waitForVlmParsing(userMsg, convId, userMsgIdx);
  saveConversations(convId);
  await startAssistantResponse(convId);
}

// ══════════════════════════════════════════════════════
//  ★ Pending Message Queue — dispatch, UI, cancel
// ══════════════════════════════════════════════════════

/**
 * Dispatch the next queued message for a conversation.
 * Called from finishStream() after a stream completes.
 */
async function _dispatchQueuedMessage(convId) {
  const queue = pendingMessageQueue.get(convId);
  if (!queue || queue.length === 0) {
    pendingMessageQueue.delete(convId);
    renderPendingQueueUI(convId);
    console.log(`%c[Queue] ∅ Queue empty for conv=${convId.slice(0,8)}, nothing to dispatch`, 'color:#6b7280');
    return;
  }
  const item = queue.shift();
  const remaining = queue.length;
  if (remaining === 0) pendingMessageQueue.delete(convId);
  renderPendingQueueUI(convId);
  updateSendButton();

  const queuedAge = Date.now() - item.timestamp;
  console.log(
    `%c[Queue] ▶ Dispatching%c queued message for conv=${convId.slice(0,8)} | waited=${(queuedAge/1000).toFixed(1)}s | text=${item.text?.length || 0}ch | remaining=${remaining}`,
    'color:#34d399;font-weight:bold', 'color:#34d399'
  );
  debugLog(`正在发送排队消息… (剩余 ${remaining} 条)`, 'info');

  const conv = conversations.find(c => c.id === convId);
  if (!conv) return;

  // Re-hydrate the user message
  const userMsg = {
    role: "user",
    content: item.text,
    images: item.images || [],
    pdfTexts: item.pdfTexts || [],
    timestamp: item.timestamp,
  };
  if (item.replyQuotes) userMsg.replyQuotes = item.replyQuotes;

  // Fetch conv ref texts if needed
  if (item.convRefs && item.convRefs.length > 0) {
    userMsg.convRefs = item.convRefs;
    const fetchPromises = item.convRefs.map(async (cr) => {
      try {
        let convMsgs = null;
        const localConv = conversations.find(c => c.id === cr.id);
        if (localConv && localConv.messages && localConv.messages.length > 0) {
          convMsgs = localConv.messages;
        } else {
          const resp = await fetch(apiUrl(`/api/conversations/${cr.id}`));
          if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
          const data = await resp.json();
          convMsgs = data.messages || [];
        }
        return { id: cr.id, title: cr.title, text: _formatConvRefText(cr.title, convMsgs) };
      } catch (e) {
        return { id: cr.id, title: cr.title, text: `[Error loading conversation: ${e.message}]` };
      }
    });
    userMsg.convRefTexts = await Promise.all(fetchPromises);
  }

  // Auto-translate: detect Chinese, defer translation to async task (non-blocking)
  // ★ FIX: Use per-conv autoTranslate, NOT the global (which reflects the currently viewed conv)
  const _convAutoTranslate = conv.autoTranslate !== undefined ? !!conv.autoTranslate : true;
  let queueNeedsTranslation = false;
  if (_convAutoTranslate && item.text) {
    const hasChinese = /[\u4e00-\u9fff\u3400-\u4dbf]/.test(item.text);
    if (hasChinese) {
      userMsg.originalContent = item.text;
      queueNeedsTranslation = true;
    }
  }

  // Push and render immediately (no blocking on translation)
  /* ★ FIX: Same _needsLoad guard as in sendMessage() — see comment there. */
  conv._needsLoad = false;
  conv.messages.push(userMsg);
  const userMsgIdx = conv.messages.length - 1;
  if (activeConvId === convId) {
    const w = document.getElementById("welcome");
    if (w) w.remove();
    const chatInnerQ = document.getElementById("chatInner");
    if (chatInnerQ) chatInnerQ.insertAdjacentHTML("beforeend", renderMessage(userMsg, userMsgIdx));
    const newEl = document.getElementById("msg-" + userMsgIdx);
    if (newEl) {
      newEl.classList.add("message-new");
      newEl.addEventListener('animationend', () => newEl.classList.remove('message-new'), { once: true });
    }
    scrollToBottom(true);
  }
  console.log(`%c[Queue] ▶ Dispatched msg rendered at idx=${userMsgIdx}, starting assistant response…`, 'color:#34d399');

  // ── Non-blocking translation: fire background, reuse shared helper ──
  if (queueNeedsTranslation) {
    conv._translating = true;
    conv._translateAborted = false;
    updateSendButton();
    renderConversationList();
    if (activeConvId === convId) {
      const msgEl = document.getElementById('msg-' + userMsgIdx);
      if (msgEl) msgEl.classList.add('user-translating');
    }
    _translateThenRespond(conv, convId, userMsg, userMsgIdx, item.text);
    return;
  }

  // ── ★ Wait for VLM parsing to complete (mandatory for PDFs) ──
  await _waitForVlmParsing(userMsg, convId, userMsgIdx);

  saveConversations(convId);
  await startAssistantResponse(convId);
}

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
 * Remove a single item from the pending queue.
 */
function removePendingQueueItem(convId, idx) {
  const queue = pendingMessageQueue.get(convId);
  if (!queue) return;
  const removed = queue[idx];
  const removedPreview = removed?.text ? removed.text.slice(0, 40) : '(attachment)';
  queue.splice(idx, 1);
  console.log(
    `%c[Queue] ✕ Removed%c item #${idx + 1} from conv=${convId.slice(0,8)} | "${removedPreview}" | remaining=${queue.length}`,
    'color:#f59e0b;font-weight:bold', 'color:#f59e0b'
  );
  if (queue.length === 0) pendingMessageQueue.delete(convId);
  renderPendingQueueUI(convId);
  updateSendButton();
  debugLog(`已取消排队消息 #${idx + 1}`, 'info');
}

/**
 * Clear all pending queued messages for a conversation.
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
}

// ══════════════════════════════════════════════════════
//  ★ Regenerate from a specific user message
// ══════════════════════════════════════════════════════
async function regenerateFromUser(idx) {
  const conv = getActiveConv();
  if (!conv || activeStreams.has(conv.id) || conv.activeTaskId) return;
  const msg = conv.messages[idx];
  if (!msg || msg.role !== "user") return;
  // Truncate all messages after this user message
  conv.messages = conv.messages.slice(0, idx + 1);
  /* ★ FIX: After truncation, clear _needsLoad and reset _serverMsgCount.
   * Without this, startAssistantResponse's _needsLoad guard reloads from DB,
   * which brings back the old assistant messages that were just truncated.
   * Also reset _serverMsgCount so syncConversationToServer's "fewer messages"
   * guard doesn't block the truncated save. */
  conv._needsLoad = false;
  conv._serverMsgCount = conv.messages.length;

  // ── Image Gen mode intercept: re-generate via direct image API ──
  // When imageGenMode is ON (or the message was an image-gen message),
  // pop the user message, fill the textarea, and call generateImageDirect().
  const _isIgConv = imageGenMode || conv.imageGenMode;
  const _isIgMsg  = msg._isImageGen || (msg.content && msg.content.startsWith('🎨 '));  // backward compat: old convs may have 🎨 prefix
  if (_isIgConv || _isIgMsg) {
    // Remove the user message so generateImageDirect() can re-add it
    conv.messages.pop();
    saveConversations(conv.id);
    renderChat(conv);
    renderConversationList();
    // Fill the textarea with the original prompt (strip 🎨 prefix if present — backward compat for old convs)
    let prompt = msg.content || '';
    if (prompt.startsWith('🎨 ')) prompt = prompt.slice(2).trim();
    const textarea = document.getElementById('userInput');
    if (textarea) { textarea.value = prompt; }
    // Ensure image gen mode is active
    if (!imageGenMode) _applyImageGenUI(true);
    generateImageDirect();
    return;
  }

  // ── ★ BLOCKING auto-translate (sync call, matches sendMessage/saveEditAndResend) ──
  // ★ Use per-conv autoTranslate (not global) — matches sendMessage behavior
  const _regenAutoTranslate = conv.autoTranslate !== undefined ? !!conv.autoTranslate : !!autoTranslate;
  let needsTranslation = false;
  if (_regenAutoTranslate && msg.content) {
    /* ★ FIX: Detect interrupted translation — when user stopped during the
     *   translation phase, originalContent is set but content was never updated
     *   to the English translation (content === originalContent, _translateDone
     *   is falsy).  In that case, we must re-translate on regen.
     *   Case 1: No originalContent → fresh Chinese text, needs translation.
     *   Case 2: originalContent exists, content === originalContent, not _translateDone
     *           → translation was interrupted before completing, needs re-translation. */
    const _translationIncomplete = msg.originalContent
      && msg.content === msg.originalContent && !msg._translateDone;
    if (!msg.originalContent || _translationIncomplete) {
      const hasChinese = /[\u4e00-\u9fff\u3400-\u4dbf]/.test(
        msg.originalContent || msg.content
      );
      if (hasChinese) {
        if (!msg.originalContent) msg.originalContent = msg.content;
        needsTranslation = true;
      }
    }
  }

  saveConversations(conv.id);

  /* ── Surgical DOM truncation: remove message nodes after idx ──
   * Instead of renderChat(conv) which wipes innerHTML (causing a
   * visible scroll-jump-to-top then snap-back), we surgically remove
   * only the DOM nodes for messages that were truncated.  This
   * preserves the scroll position entirely — zero visual glitch.
   * Falls back to full renderChat if the DOM state is unexpected. */
  let usedSurgical = false;
  if (activeConvId === conv.id) {
    const inner = document.getElementById("chatInner");
    if (inner) {
      /* Collect message elements with idx > the regen point */
      const toRemove = [];
      inner.querySelectorAll('.message[id^="msg-"]').forEach(el => {
        const m = el.id.match(/^msg-(\d+)$/);
        if (m && parseInt(m[1], 10) > idx) toRemove.push(el);
      });
      /* Also remove any leftover streaming bubble */
      const oldStreaming = document.getElementById("streaming-msg");
      if (oldStreaming) toRemove.push(oldStreaming);

      if (toRemove.length > 0 || inner.querySelector('.message[id^="msg-"]')) {
        /* Remove in a single batch — no reflow between removals */
        for (const el of toRemove) el.remove();
        usedSurgical = true;
        _lastRenderedFingerprint = _convRenderFingerprint(conv);
        buildTurnNav(conv);
      }
    }
  }
  if (!usedSurgical) {
    renderChat(conv);
  }

  renderConversationList();

  // ── ★ NON-BLOCKING auto-translate: fire background, free UI immediately ──
  if (needsTranslation) {
    const convId = conv.id;
    conv._translating = true;
    conv._translateAborted = false;
    if (typeof updateSendButton === 'function') updateSendButton();
    renderConversationList();
    if (activeConvId === convId) {
      const msgEl = document.getElementById('msg-' + idx);
      if (msgEl) msgEl.classList.add('user-translating');
    }
    _translateThenRespond(conv, convId, msg, idx, msg.originalContent);
    return;
  }

  /* ★ FIX: Persist the truncated messages to server BEFORE starting the new task. */
  await syncConversationToServer(conv);
  await startAssistantResponse(conv.id);
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
//  2. Roll back searchRounds, content, and thinking to the checkpoint.
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
    await syncConversationToServer(conv);
    await startAssistantResponse(conv.id);
    return;
  }

  // ═══════════════════════════════════════════════════════════
  // ★ Step 1: Find the latest recoverable checkpoint
  //   Scan searchRounds to find complete tool batches.
  //   A "complete" round has toolCallId, status==="done", and toolContent.
  // ═══════════════════════════════════════════════════════════
  const allRounds = getSearchRoundsFromMsg(assistantMsg);
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
        if (r.toolContent == null || r.status !== "done") {
          // Incomplete — stop here, don't include this or anything after
          debugLog(
            `Tool round #${r.roundNum} (${r.toolName}) incomplete — checkpoint before it`,
            "warn",
          );
          break;
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
    await syncConversationToServer(conv);
    await startAssistantResponse(conv.id);
    return;
  }

  // ═══════════════════════════════════════════════════════════
  // ★ Step 3: Roll back to checkpoint
  //   - Keep only complete tool rounds in searchRounds
  //   - Discard partial content and thinking (the LLM will regenerate)
  //   - The user sees the tool call history preserved, text regenerated
  // ═══════════════════════════════════════════════════════════
  const keptRounds = allRounds.slice(0, lastCompleteIdx + 1);
  const discardedRounds = allRounds.length - keptRounds.length;

  // ★ FIX: Reconstruct content prefix from completed rounds' assistantContent
  //   instead of wiping everything to "".  Each kept round's assistantContent
  //   is the text the LLM wrote alongside that tool call batch — preserving it
  //   means the user doesn't lose visible output from successful prior rounds.
  const preservedContent = keptRounds
    .map(r => r.assistantContent || "")
    .filter(c => c)
    .join("\n\n");
  const originalContent = assistantMsg.content || "";
  const discardedContent = Math.max(0, originalContent.length - preservedContent.length);
  const discardedThinking = (assistantMsg.thinking || "").length;

  assistantMsg.searchRounds = keptRounds;
  assistantMsg.content = preservedContent;
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
    `(preserved ${preservedContent.length} chars from completed rounds), ` +
    `discarded ${discardedRounds} incomplete rounds + ` +
    `${discardedContent} chars new content + ${discardedThinking} chars thinking`,
    "info",
  );
  if (discardedRounds > 0 || discardedContent > 0) {
    const preserveNote = preservedContent.length > 0
      ? ` (保留了 ${preservedContent.length} 字符已完成工具调用的内容)`
      : '';
    showToast(
      `从第 ${keptRounds.length} 轮工具调用后恢复${preserveNote}${discardedContent > 0 ? `，丢弃了 ${discardedContent} 字符后续文本` : ''}${discardedRounds > 0 ? (discardedContent > 0 ? ' + ' : '，丢弃了 ') + discardedRounds + ' 个未完成工具调用' : ''}`,
      "info",
    );
  }

  // Save pre-checkpoint apiRounds & usage for merging after completion
  assistantMsg._continueApiRounds = (assistantMsg.apiRounds || []).slice();
  if (assistantMsg.usage)
    assistantMsg._continueUsage = { ...assistantMsg.usage };
  // Save the checkpoint searchRounds so we can merge with new ones
  assistantMsg._continueSearchRounds = keptRounds.slice();
  // ★ Save modifiedFiles/modifiedFileList for merging after completion
  if (assistantMsg.modifiedFiles)
    assistantMsg._continueModifiedFiles = assistantMsg.modifiedFiles;
  if (assistantMsg.modifiedFileList)
    assistantMsg._continueModifiedFileList = (assistantMsg.modifiedFileList || []).slice();

  // ═══════════════════════════════════════════════════════════
  // ★ Step 4: Build messages — EXCLUDE the trailing assistant message
  // ═══════════════════════════════════════════════════════════
  const apiMessages = buildApiMessages(conv); // excludes last msg (assistant)

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
  // ═══════════════════════════════════════════════════════════
  const cfgPayload = {
    preset: config.model || serverModel,
    model: config.model || serverModel,
    thinkingEnabled,
    thinkingDepth: config.thinkingDepth,
    temperature: config.temperature,
    searchMode,
    fetchEnabled,
    codeExecEnabled,
    skillsEnabled,
    /* ★ FIX: read from per-conv state, not global projectState (same race as startAssistantResponse) */
    projectPath: _getConvProjectPath(conv),
    autoApply: autoApplyWrites,
    browserEnabled,
  };
  if (toolHistory.length > 0) {
    cfgPayload.toolHistory = toolHistory;
  }
  // ★ Send preserved content prefix so backend checkpoints include it
  if (preservedContent) {
    cfgPayload.contentPrefix = preservedContent;
  }
  debugLog(
    `Continue: sending ${toolHistory.length} tool round(s) as checkpoint` +
    `${preservedContent ? ` + ${preservedContent.length} chars preserved content` : ''}, ` +
    `LLM will regenerate content fresh from tool results`,
    "info",
  );

  let taskId;
  try {
    const res = await fetch(apiUrl("/api/chat/start"), {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        convId: conv.id,
        messages: apiMessages,
        config: cfgPayload,
      }),
    });
    const data = await res.json();
    if (!res.ok) throw new Error(data.error || "Request failed");
    taskId = data.taskId;
  } catch (e) {
    debugLog("Continue failed: " + e.message, "error");
    return;
  }
  conv.activeTaskId = taskId;
  saveConversations(conv.id);
  syncConversationToServer(conv);
  connectToTask(conv.id, taskId);
}

/**
 * ★ Build a single tool history round from a batch of searchRound entries.
 * Each round represents one assistant message with tool_calls + their results.
 */
function _buildToolHistoryRound(batch) {
  const round = {
    assistantContent: "",
    toolCalls: [],
    toolResults: [],
  };
  // ★ Pick up per-round assistantContent from the first entry in the batch
  //   (tagged by the orchestrator when the LLM emitted text alongside tool calls)
  for (const r of batch) {
    if (!round.assistantContent && r.assistantContent) {
      round.assistantContent = r.assistantContent;
    }
    round.toolCalls.push({
      id: r.toolCallId,
      name: r.toolName,
      arguments: r.toolArgs || "{}",
    });
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
      _populateModelDropdown(models);
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
/* Backward-compat alias */
function selectPreset(presetOrModel) { selectModel(presetOrModel); }
function cyclePreset() {
  /* Cycle through visible (non-hidden, chat-only) registered models */
  const models = _registeredModels.filter(m => {
    if (_hiddenModels.has(m.model_id)) return false;
    var caps = m.capabilities || [];
    for (var i = 0; i < caps.length; i++) {
      if (caps[i] === 'image_gen' || caps[i] === 'embedding') return false;
    }
    return true;
  }).map(m => m.model_id);
  if (models.length === 0) return;
  const cur = config.model || '';
  const idx = models.indexOf(cur);
  selectModel(models[(idx + 1) % models.length]);
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
  // AI enhance: codeExec, skills, translate
  const aiCount = (codeExecEnabled ? 1 : 0) + (skillsEnabled ? 1 : 0) + (autoTranslate ? 1 : 0);
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

function handleKeyDown(e) {
  if (e.key === "Enter" && e.ctrlKey) {
    e.preventDefault();
    // Route to image generation when in image gen mode
    if (imageGenMode) { generateImageDirect(); }
    else { sendMessage(); }
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

// ── Sidebar search ──
function initSidebarSearch() {
  const input = document.getElementById("sidebarSearchInput");
  let timer = null;
  input.addEventListener("input", () => {
    clearTimeout(timer);
    timer = setTimeout(() => {
      sidebarSearchQuery = input.value.trim().toLowerCase();
      document
        .getElementById("sidebarSearchClear")
        .classList.toggle("visible", sidebarSearchQuery.length > 0);
      renderConversationList();
    }, 300);
  });
  input.addEventListener("keydown", (e) => {
    if (e.key === "Escape") {
      e.preventDefault();
      clearSidebarSearch();
      input.blur();
    }
  });
}
function clearSidebarSearch() {
  const input = document.getElementById("sidebarSearchInput");
  input.value = "";
  sidebarSearchQuery = "";
  document.getElementById("sidebarSearchClear").classList.remove("visible");
  document.getElementById("sidebarSearchStats").classList.remove("visible");
  renderConversationList();
  input.focus();
}

// ── Init ──
async function initActiveTasks() {
  try {
    /* ── Parallel fetch: metadata + active tasks ── */
    /* Pass activeConvId (or restored conv from sessionStorage) to prefetch
       its messages in the same request, eliminating the second round-trip
       that shows "loading..." */
    const prefetchTarget = activeConvId || sessionStorage.getItem('chatui_activeConvId') || null;
    const [, activeResp] = await Promise.all([
      loadConversationsFromServer(prefetchTarget),
      fetch(apiUrl("/api/chat/active")),
    ]);
    if (!activeResp.ok) {
      _ensureNewest();
      return;
    }
    const serverTasks = await activeResp.json();
    const runIds = new Set(
      serverTasks.filter((t) => t.status === "running").map((t) => t.id),
    );
    const toRecon = [];
    /* ★ Build a map from convId → running taskId for orphan recovery
          (handles the case where user refreshed before activeTaskId was saved) */
    const convIdToRunningTask = new Map();
    for (const t of serverTasks) {
      if (t.status === "running" && t.convId) {
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
            searchRounds: [],
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
          syncConversationToServer(conv);
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

    /* ── Batch-poll finished tasks in parallel ── */
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
                if (td.searchRounds) am.searchRounds = td.searchRounds;
                if (td.finishReason) am.finishReason = td.finishReason;
                if (td.usage) am.usage = td.usage;
                if (td.preset) am.preset = td.preset;
                else if (td.effort) am.preset = td.effort;
                if (td.fallbackModel) am.fallbackModel = td.fallbackModel;
                if (td.fallbackFrom) am.fallbackFrom = td.fallbackFrom;
                if (td.modifiedFiles) am.modifiedFiles = td.modifiedFiles;
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
          for (const m of conv.messages) {
            if (m.searchRounds) {
              for (const r of m.searchRounds) {
                if (r.status === 'awaiting_human' || r.status === 'submitted') {
                  r.status = 'done';
                  r.guidanceId = null;
                  r._hgSkipped = true;
                  _hgCleaned++;
                }
              }
            }
          }
          if (_hgCleaned > 0) {
            console.info(`[initActiveTasks CaseB] 🧹 Cleaned ${_hgCleaned} orphaned HG round(s) — conv=${conv.id.slice(0,8)}`);
          }
          conv.activeTaskId = null;
          conv._activeTaskClearedAt = Date.now();
          saveConversations(conv.id);
          syncConversationToServer(conv);
        }),
      );
    }

    renderConversationList();
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

    _ensureNewest();
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
    .getElementById("skillsToggle")
    ?.classList.toggle("active", skillsEnabled);
  document
    .getElementById("skillsBadge")
    ?.classList.toggle("visible", skillsEnabled);
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
    const del = e.target.closest(".conv-delete");
    if (del) {
      e.stopPropagation();
      if (del.dataset.convId) deleteConversation(del.dataset.convId, e);
      return;
    }
    const pin = e.target.closest(".conv-pin");
    if (pin) {
      e.stopPropagation();
      if (pin.dataset.convId) togglePinConversation(pin.dataset.convId);
      return;
    }
    // ★ @ reference button — add conversation reference chip
    const ref = e.target.closest(".conv-ref");
    if (ref) {
      e.stopPropagation();
      if (ref.dataset.convId) {
        addConvRef(ref.dataset.convId, ref.dataset.convTitle || "Untitled");
      }
      return;
    }
    const item = e.target.closest(".conv-item");
    if (item && item.dataset.convId) loadConversation(item.dataset.convId);
  }
  document
    .getElementById("convList")
    .addEventListener("click", _handleConvClick);
  document
    .getElementById("convPinnedZone")
    .addEventListener("click", _handleConvClick);
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
  // ── Full-page drag & drop ──
  let _dragCounter = 0;
  const overlay = document.getElementById("dropOverlay");
  document.addEventListener("dragenter", (e) => {
    e.preventDefault();
    if (!e.dataTransfer || !e.dataTransfer.types.includes("Files")) return;
    _dragCounter++;
    if (_dragCounter === 1 && overlay) overlay.classList.add("visible");
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
    if (e.key === "Escape") {
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
