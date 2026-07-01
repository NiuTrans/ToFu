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
    toggle.setAttribute("data-brand", String(brand));
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
  /* ★ Refresh the context health bar — the model's context window changed,
   * so the same token count maps to a different fill % and zone. */
  if (typeof updateContextBar === 'function') updateContextBar();
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

  /* ★ Bail if the input area is hidden (e.g. Paper Reading Mode sets
   * .input-area display:none). While hidden, every child's
   * getBoundingClientRect() returns width 0, so the measured sum collapses
   * and we'd write the Math.max(480) floor → a scrunched toolbar that
   * persists after returning to chat. Skip entirely; exitPaperMode()
   * re-runs the reflow once the area is visible again. */
  if (inputInner.offsetParent === null) return;

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
  // 'single' is a retired mode — fold legacy values into 'multi'.
  if (mode === "single") mode = "multi";
  const modes = ["off", "multi"];
  if (!modes.includes(mode)) mode = "off";
  searchMode = mode;
  const titles = {
    off: "Search",
    multi: "Search",
  };
  const labels = { off: "OFF", multi: "∞" };
  const badgeTexts = { multi: "∞ MULTI SEARCH" };
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
  if (hint) hint.innerHTML = _renderHintHtml(imageGenMode
    ? t('ig.hint')
    : _inputSendHintText());
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
  conv.autopilotEnabled = !!autopilotEnabled;
  conv.activeFlow = activeFlow || '';
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
  _toolStateDebounceTimers.set(conv.id, setTimeout(async () => {
    _toolStateDebounceTimers.delete(conv.id);
    try {
      const settings = await _buildConvSettings(conv);
      const resp = await Api.chat.patchToolState(conv.id, settings);
      if (!resp || !resp.ok) console.warn(`[ToolState] PATCH failed: HTTP ${resp ? resp.status : 'no response'}`);
    } catch (err) {
      console.warn(`[ToolState] PATCH error: ${err.message}`);
    }
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
  _applyAutopilotUI(!!conv.autopilotEnabled);
  _applyFlowUI(conv.activeFlow || '');
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
  /* ★ Auto-translate toggle: restore the per-conv frozen value, BUT never pull
   *   a live global-ON down to a conv that was frozen OFF — otherwise opening
   *   an old conversation silently turns the user's global "auto-translate on"
   *   off, and the on-open retro-translate (convAutoTranslateEffective, which
   *   reads this same global) never fires. When the global is ON we keep it ON;
   *   the per-conv freeze still governs the in-flight send path via
   *   convAutoTranslate. */
  if (typeof autoTranslate !== 'undefined' && autoTranslate) {
    _applyAutoTranslateUI(true);
  } else {
    _applyAutoTranslateUI(convAutoTranslate(conv));
  }
  if (typeof updateSubmenuCounts === 'function') updateSubmenuCounts();
  if (typeof updateContextBar === 'function') updateContextBar();
  /* ★ Re-filter the cross-conversation presence strip to this conversation's
   *   project root immediately on switch (else it lags up to one 5s tick). */
  if (typeof presenceRefresh === 'function') presenceRefresh();
  /* ★ If the Project Brain panel is open, re-resolve its feed to the new
   *   conversation's project (two projects must never bleed into one view). */
  if (typeof projectBrainRefresh === 'function') projectBrainRefresh();
  /* ★ Reflow toolbar after restoring conv tool state (toolbar width may differ). */
  _scheduleReflow();
}
function _resetToolsToDefaults() {
  config.thinkingDepth = config.defaultThinkingDepth;   // ← reset to default depth BEFORE applying model UI (let _applyModelUI normalize)
  _applyModelUI(serverModel);
  _applySearchModeUI("multi");
  _applyFetchEnabledUI(true);
  _applyCodeExecUI(false);
  _applyBrowserUI(false);
  _applyMemoryUI(true);
  _applySwarmUI(true);
  _applyEndpointUI(false);
  _applyAutopilotUI(false);
  _applyFlowUI('');
  _applyImageGenToolUI(false);
  _applyImageGenUI(false);
  if (typeof paperMode !== 'undefined' && paperMode && typeof exitPaperMode === 'function') exitPaperMode();
  _applyAutoTranslateUI(convAutoTranslate(null));
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

/* ═══════════════════════════════════════════════════════════════════
   Functions originally defined here (newChat, loadConversation,
   sendMessage, regenerateFromUser, initActiveTasks, ...) live in the
   `static/js/main/` subpackage. The bundler concatenates them BEFORE
   this file (see `lib/js_bundler.py::_BUNDLE_FILES`), so by the time
   the boot IIFE below runs every symbol is in window scope.
   ═══════════════════════════════════════════════════════════════════ */


// ── Event bindings ──
(function init() {
  try {
  // ── Init model toggle from config ──
  (function initModelToggle() {
    thinkingEnabled = true;
    _applyModelUI(config.model || serverModel);
    _loadServerConfigAndPopulate();
  })();
  // Apply stored input-send-mode hint text on load
  try { refreshInputSendHint(); } catch (_) {}
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
  document
    .getElementById("swarmToggle")
    ?.classList.toggle("active", swarmEnabled);
  {
    const _swarmBadge = document.getElementById("swarmBadge");
    if (_swarmBadge) _swarmBadge.style.display = swarmEnabled ? "" : "none";
  }
  if (typeof updateSubmenuCounts === "function") updateSubmenuCounts();
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
    // ★ Rename conversation button — inline title edit dialog
    const rename = e.target.closest(".conv-rename");
    if (rename) {
      e.stopPropagation();
      if (rename.dataset.convId) _promptRenameConversation(rename.dataset.convId);
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
  /* ★ PERF (INP): autosize forces a synchronous layout (set height=auto, read
   * scrollHeight). Doing it inline on every keystroke thrashes layout and shows
   * up as a long "pointer/keydown" task. Coalesce the read+write into a single
   * rAF so rapid typing batches to one layout pass per frame. */
  let _taResizePending = false;
  const _autosizeTextarea = () => {
    _taResizePending = false;
    ta.style.height = "auto";
    ta.style.height = Math.min(ta.scrollHeight, 200) + "px";
  };
  ta.addEventListener("input", () => {
    if (!_taResizePending) {
      _taResizePending = true;
      requestAnimationFrame(_autosizeTextarea);
    }
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
    // Detect log noise in pasted text — server-side via /api/v1/logs/clean
    if (!hasImage) {
      const pastedText = e.clipboardData?.getData("text");
      if (pastedText && pastedText.length > 200) {
        setTimeout(async () => {
          const result = await detectLogNoise(ta.value);
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
  const _restoredConvId = sessionStorage.getItem('tofu_activeConvId') || null;
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
  loadProjectStatus();
  _updateAutoApplyUI();
  _applyAutoTranslateUI();
  setInterval(() => {
    if (document.visibilityState !== "visible" || _editingMsgIdx !== null) return;
    /* Yield to pending input: the merge + conv-list rebuild is ~tens of ms of
     * main-thread work; running it on a bare timer adds input delay (poor INP)
     * when a click lands mid-poll. requestIdleCallback defers it until the main
     * thread is free. Fallback to a plain call where rIC is unavailable. */
    const _poll = () => loadConversationsFromServer();
    if (typeof requestIdleCallback === "function")
      requestIdleCallback(_poll, { timeout: 5000 });
    else
      _poll();
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
