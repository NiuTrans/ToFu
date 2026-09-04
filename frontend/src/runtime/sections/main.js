/* ===== migrated source: main.js ===== */
/* ═══════════════════════════════════════════
   main.js — Chat Core, Toolbar, Init
   Composer orchestrator: submits Turn commands and manages catalog settings,
   wires toolbar UI, and boots the app.
   Feature modules live in separate files:
     image-gen.js, log-clean.js, translation.js,
     upload.js, project.js, memory.js, myday.js
   ═══════════════════════════════════════════ */

/* pendingPdfTexts → defined in upload.js */
/* _pendingLogClean → defined in log-clean.js */

// ── Conversation CRUD ──
function _purgeEmptyConvs() {
  const before = conversations.length;
  const purged = [];
  conversations = conversations.filter((c) => {
    /* A conv carrying a project mount is NEVER "empty": the attachment is
     *   deliberate per-conv state that exists ONLY client-side until the first
     *   message lands (both sync paths skip 0-message convs), so purging it
     *   here is unrecoverable — every switch away silently destroyed the new
     *   conversation AND its project bar state (2026-08-21 report: "switching
     *   conversations loses the project bar's per-conv state"; frontend.log
     *   showed the user re-attaching via mpApplyFolders 4× in 7 minutes). */
    const turnCount = runtimeScope.ConversationTurnRead?.ordered?.(c)?.length || 0;
    const keep = turnCount > 0 || c.id === activeConvId || (c._serverTurnCount || 0) > 0 || c._turnSnapshotRequired
      || !!c.projectPath;
    if (!keep) purged.push(`${c.id.slice(0,8)}(turns=${turnCount},srv=${c._serverTurnCount||0},snapshotRequired=${!!c._turnSnapshotRequired})`);
    return keep;
  });
  if (purged.length > 0) {
    console.warn(`[_purgeEmptyConvs] Purged ${purged.length} empty convs: ${purged.join(', ')}`);
  }
}
// ── Per-conversation tool state helpers ──
/* ── Brand detection for model_id — typed owner alias from _prelude.js ── */
const _DEPTH_ICONS  = { off: '', medium: '', high: '', xhigh: '', max: '', ultra: '' };
const _DEPTH_ICON_FALLBACK = '';
const _DEPTH_LABELS = { off: 'Off', medium: 'Med', high: 'High', xhigh: 'xHigh', max: 'Max', ultra: 'Ultra' };
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
/* Registered model list — replaced by the authenticated v2 projection at boot. */
let _registeredModels = [];   // [{ model_id, brand, thinking_default, capabilities }]
let _registeredModelsLoaded = false;
/* Hidden models — loaded from server config, not shown in dropdown */
let _hiddenModels = new Set();
function _paperModelPickerState() {
  return {
    models: _registeredModels,
    hiddenModels: _hiddenModels,
    selectedModel: (config && config.model) || serverModel || '',
  };
}
/* Hidden image gen models — loaded from server config, not shown in image gen picker */
var _hiddenIgModels = new Set();  // shared with image-gen.js

/* _modelShortName is composed from core/model-display-names in _prelude.js. */

/* Track what _applyModelUI last applied so we can skip redundant work */
let _lastAppliedModelId = null;
let _lastAppliedIsThinking = null;

function _applyModelUI(modelId) {
  /* SINGLE RESOLVER. This function — and ONLY this function — decides what
   *   to paint when a conversation has no stored model. Callers MUST pass the
   *   conversation's own value (or nothing at all); they must NOT pre-resolve
   *   `|| serverModel` themselves.
   *
   *   Why it matters: the write-back sites persist `config.model`, so the
   *   composer's value becomes the conversation's stored identity. A model id
   *   is therefore EITHER an explicit choice (selectModel / a value restored
   *   from the conv) OR a mere DISPLAY fallback. Only this function can tell
   *   the two apart — and only if the fallback is applied HERE. When a caller
   *   pre-resolved `conv.model || serverModel`, every value arrived truthy,
   *   `_modelIsProvisional` was permanently false, and the guard below it was
   *   dead code: the kimi-k3 default was still laundered into storage
   *   (2026-07-27, conv ms352oniikgq10 — an Opus 5 conversation whose composer
   *   painted the global default, which the next tool-toggle then PATCHed over
   *   the real stored model).
   *
   *   Contract: falsy `modelId` => "this conversation stored nothing" => paint
   *   the default but mark it PROVISIONAL so it is never written down. */
  const _provisional = !modelId;
  if (!modelId) modelId = serverModel;
  /* Legacy preset migration */
  if (typeof _LEGACY_PRESET_TO_MODEL !== 'undefined' && _LEGACY_PRESET_TO_MODEL[modelId]) {
    modelId = _LEGACY_PRESET_TO_MODEL[modelId];
  }
  config.model = modelId;
  config._modelIsProvisional = _provisional;
  const brand = typeof _detectBrand === 'function' ? _detectBrand(modelId) : 'generic';
  const shortName = _modelShortName(modelId);
  const isThinking = _isThinkingCapable(modelId);
  /* Ensure thinkingDepth is always set for thinking models, null for non-thinking.
   * This prevents the || "medium" fallback from leaking depth to non-thinking models. */
  if (isThinking) {
    config.thinkingDepth = config.thinkingDepth || config.defaultThinkingDepth;
  } else {
    config.thinkingDepth = null;
  }
  const depth = config.thinkingDepth || config.defaultThinkingDepth;
  /* Set thinkingEnabled based on depth: 'off' disables thinking even for thinking-capable models */
  thinkingEnabled = isThinking && depth !== 'off';

  /* PERF: Skip all DOM work + reflow if model hasn't actually changed.
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

  // Update model badge
  const modelBadge = document.getElementById("modelBadge");
  if (modelBadge) {
    if (isThinking && depth !== 'off') {
      modelBadge.innerHTML = `${shortName} &middot; ${_DEPTH_ICONS[depth] || _DEPTH_ICON_FALLBACK} ${_DEPTH_LABELS[depth] || depth}`;
    } else {
      modelBadge.textContent = shortName;
    }
  }

  // Update toggle button
  const toggle = document.getElementById("presetToggle");
  if (toggle) {
    toggle.setAttribute("data-model", modelId);
    toggle.setAttribute("data-brand", String(brand));
    const iconEl = toggle.querySelector(".ps-icon");
    const labelEl = toggle.querySelector(".ps-label");
    if (labelEl) {
      /* Don't show depth in toggle label — the depth bar buttons right next to it
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

  // Highlight active model item in dropdown
  document.querySelectorAll(".preset-dropdown-item").forEach((item) => {
    const sameModel = item.getAttribute("data-value") === modelId;
    const routeKey = item.getAttribute('data-route-key') || '';
    const rowProvider = routeKey ? routeKey.split('::', 1)[0] : '';
    const preferred = config.preferredProviderId || 'auto';
    item.classList.toggle('active', sameModel && (!routeKey || rowProvider === preferred));
  });
  _updateDepthButtons(depth);

  /* Show/hide the thinking-depth section. It now lives INSIDE the model
   * dropdown (folded out of the toolbar row), so its visibility no longer
   * changes the toolbar's intrinsic width — no reflow is needed for it, and
   * the row-fade transition machinery is gone. Just show it for
   * thinking-capable models, hide it otherwise. */
  const depthBar = document.getElementById("thinkingDepthSection");
  if (depthBar) depthBar.style.display = isThinking ? '' : 'none';
  document.getElementById("presetWrapper")?.classList.remove("open");
  /* Resize .input-inner to fit toolbar content — the model label width may
   * have changed. (Depth visibility no longer affects width — see above.) */
  _scheduleReflow();
  /* Refresh the context health bar — the model's context window changed,
   * so the same token count maps to a different fill % and zone. */
  if (typeof runtimeScope.updateContextBar === 'function') runtimeScope.updateContextBar();
}
/* _scheduleReflow: coalesce multiple _reflowToolbar requests into a single
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

/* _reflowToolbar: measure the toolbar's natural (unwrapped) width, then set
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

  /* Bail if the input area is hidden (e.g. Paper Reading Mode sets
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
  /* Floor the composer to the chat reading column so the input box lines up
   * with the Turns above it, instead of collapsing to the (now much
   * narrower) decluttered-toolbar content width — on a wide landscape display
   * a ~540px toolbar under an 820px message column reads as "input too narrow".
   * Read `--msg-measure`, the SINGLE source of the reading measure, so the
   * composer tracks the message column per resolution; the measured toolbar
   * width can still EXPAND beyond it when the content genuinely needs more
   * room. The min(…,maxW) cap still wins on a narrow window so we never
   * overflow the viewport.
   *
   * ⚠️ DO NOT read `.chat-inner`'s max-width here. It used to be the measure,
   * but .chat-inner is now a three-track grid ([text][gap][rail]) whose
   * max-width is the measure PLUS the rail furniture — reading it floored the
   * composer at ~1178px under an 820px message column, i.e. an input box
   * visibly wider than the text it belongs to. Measured desync in every
   * roomy state before this changed; guarded by the composer-alignment
   * assertion in tests/test_turn_ctx_rail_geometry.py. */
  let readingFloor = 820;
  const chatInner = document.querySelector('.chat-inner');
  if (chatInner) {
    const mw = parseFloat(
      getComputedStyle(chatInner).getPropertyValue('--msg-measure'));
    if (mw && isFinite(mw)) readingFloor = mw;
  }
  w = Math.min(Math.max(w, readingFloor), maxW);
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
/* Re-measure toolbar after web fonts finish loading — font-display:swap
 * means the first _reflowToolbar may measure with fallback font metrics.
 * Once the real font loads, glyph widths change and the preset label
 * may need more space. */
if (document.fonts && document.fonts.ready) {
  document.fonts.ready.then(() => {
    /* Suppress the .input-inner max-width .4s transition for THIS re-snap.
     * With preload the metrics usually already match (no-op), but on a cold
     * cache the glyph widths still change here — without this guard the width
     * delta animates as a visible 0.4s "settle". Restore the transition on the
     * next frame so genuine user-driven width changes still animate. */
    const inner = document.querySelector('.input-inner');
    const prev = inner ? inner.style.transition : null;
    if (inner) inner.style.transition = 'none';
    _reflowToolbar();
    if (inner) {
      requestAnimationFrame(() => { inner.style.transition = prev || ''; });
    }
  });
}
/* _mirrorRailGeometry: copy the rail grant (--rail-w/--rail-gap) from
 * .chat-inner onto .input-inner, so the composer's left-edge lock
 * (styles.css `.input-inner` margin-left) replays .chat-inner's centering
 * math with the SAME furniture values. The chatpane container query that
 * decides the grant only reaches .chat-inner's subtree and CANNOT be
 * extended to a composer ancestor: container-type:inline-size carries
 * layout containment, which would re-parent the mobile bottom-sheet
 * dropdowns' position:fixed (≤768px .preset-dropdown) from the viewport to
 * the band. The ResizeObserver covers every grant flip (sidebar toggle,
 * drawer open/close, window resize): a flip only happens when
 * .chat-container's inline size crosses the threshold, which IS a resize. */
function _mirrorRailGeometry() {
  const chatInner = document.querySelector('.chat-inner');
  const inputInner = document.querySelector('.input-inner');
  if (!chatInner || !inputInner) return;
  const cs = getComputedStyle(chatInner);
  const rw = (cs.getPropertyValue('--rail-w') || '').trim() || '0px';
  const rg = (cs.getPropertyValue('--rail-gap') || '').trim() || '0px';
  /* Skip the write when unchanged — a no-op setProperty still invalidates
   * style, and the RO fires per frame during the drawer/sidebar animation. */
  if (inputInner.style.getPropertyValue('--rail-w') !== rw)
    inputInner.style.setProperty('--rail-w', rw);
  if (inputInner.style.getPropertyValue('--rail-gap') !== rg)
    inputInner.style.setProperty('--rail-gap', rg);
}
function _installRailGeometryMirror() {
  _mirrorRailGeometry();
  const cont = document.querySelector('.chat-container');
  if (cont && typeof ResizeObserver === 'function') {
    try { new ResizeObserver(_mirrorRailGeometry).observe(cont); }
    catch (err) { console.warn('rail-geometry mirror ResizeObserver failed:', err); }
  }
}

/* ── Thinking Depth Selection ── */
function selectThinkingDepth(depth) {
  config.thinkingDepth = depth;
  /* Sync thinkingEnabled: 'off' disables thinking for thinking-capable models */
  thinkingEnabled = depth !== 'off';
  /* PERF: Lightweight path — only update depth-related UI elements.
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
  /* FIX: Persist depth to conv object immediately.  Without this,
   * conv.thinkingDepth stays stale (e.g. 'off') while config.thinkingDepth
   * is updated (e.g. 'max').  If an async operation like
   * loadConversationCatalog triggers restoreConversationSettingsToComposer
   * before the user sends, it clobbers config.thinkingDepth back to
   * conv.thinkingDepth
   * → backend receives the stale depth → no thinking generated. */
  captureActiveConversationSettings();
  try { localStorage.setItem("claude_client_config", JSON.stringify(_configForPersist())); }
  catch (e) { debugLog(`[selectThinkingDepth] localStorage save failed: ${e.message}`, 'error'); }
}

function _updateDepthButtons(activeDepth) {
  /* PERF: Cache the depth button NodeList and use a for-loop instead of
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
  // fetch is bundled with search — auto-enable when search is on
  if (mode !== "off") {
    _applyFetchEnabledUI(true);
  }
}
function _applyFetchEnabledUI(enabled) {
  fetchEnabled = true; // always on — no longer toggleable
}
function _paintToolExposureState(element, exposed, available = true) {
  if (!element) return;
  // Whether an inactive tool can still be found by its internal name is an
  // implementation detail, not a second state the user needs to reason about.
  // Keep the normal checked/unchecked treatment and only surface a badge when
  // the capability is genuinely unavailable.
  element.classList.remove('discoverable');
  element.classList.toggle('tool-unavailable', !available);
  element.dataset.toolExposure = !available
    ? 'blocked' : (exposed ? 'visible' : 'searchable');
  if (available) {
    delete element.dataset.exposureLabel;
  } else {
    element.dataset.exposureLabel = typeof t === 'function'
      ? t('toolbar.toolExposureBlocked')
      : '不可用';
  }
}
if (typeof window !== 'undefined') {
  runtimeScope._paintToolExposureState = _paintToolExposureState;
}
function _applyCodeExecUI(enabled) {
  codeExecEnabled = !!enabled;
  const row = document.getElementById("codeExecToggle");
  row?.classList.toggle("active", codeExecEnabled);
  _paintToolExposureState(row, codeExecEnabled);
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
  /* The browser bridge now shares ONE toolbar entry with the desktop agent
   * (#localControlToggle). Its summary badge counts both capabilities, so it
   * is repainted here rather than toggled per-flag. */
  if (typeof _lcUpdateBadge === "function") _lcUpdateBadge();
}
function _applyMemoryUI(enabled) {
  memoryEnabled = !!enabled;
  document
    .getElementById("memoryToggle")
    ?.classList.toggle("active", memoryEnabled);
  document
    .getElementById("memoryBadge")
    ?.classList.toggle("visible", memoryEnabled);
  /* memory.js is DEFERRED (Epic-E sub-9): the modal button painter is
   * absent until the lazy ESM domain lands — gate, or the composer-settings
   * application path throws ReferenceError. */
  if (typeof _updateMemoryModalBtn === 'function') _updateMemoryModalBtn();
}
function _applyImageGenToolUI(enabled) {
  imageGenEnabled = !!enabled;
  const row = document.getElementById("imageGenToggle");
  row?.classList.toggle("active", imageGenEnabled);
  _paintToolExposureState(row, imageGenEnabled);
}
function toggleImageGenTool() {
  _applyImageGenToolUI(!imageGenEnabled);
  captureActiveConversationSettings();
  if (typeof updateSubmenuCounts === 'function') updateSubmenuCounts();
  debugLog(`Image Gen Tool: ${imageGenEnabled ? 'ON' : 'OFF'}`, imageGenEnabled ? 'success' : 'info');
}
function _applyHumanGuidanceUI(enabled) {
  humanGuidanceEnabled = !!enabled;
  const row = document.getElementById("humanGuidanceToggle");
  row?.classList.toggle("active", humanGuidanceEnabled);
  _paintToolExposureState(row, humanGuidanceEnabled);
}
function toggleHumanGuidance() {
  const disablingRequiredPlanGuidance = humanGuidanceEnabled && planMode;
  if (disablingRequiredPlanGuidance
      && typeof _agentInteractionChangeBlocked === 'function'
      && _agentInteractionChangeBlocked()) return false;
  _applyHumanGuidanceUI(!humanGuidanceEnabled);
  if (!humanGuidanceEnabled && planMode
      && typeof _applyPlanModeUI === 'function') {
    _applyPlanModeUI(false);
  }
  captureActiveConversationSettings();
  if (typeof updateSubmenuCounts === 'function') updateSubmenuCounts();
  debugLog(`Human Guidance: ${humanGuidanceEnabled ? 'ON' : 'OFF'}`, humanGuidanceEnabled ? 'success' : 'info');
  return true;
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
    : t('chat.messagePlaceholder');
  const hint = document.getElementById('inputHint');
  if (hint) hint.innerHTML = _renderHintHtml(imageGenMode
    ? t('ig.hint')
    : _inputSendHintText());
  /* Reflow toolbar only if the mode actually changed — switching between
   * ig-active / normal swaps the visible toolbar so re-measure is needed.
   * But on conv switch where both convs have imageGenMode=false, skip. */
  if (prev !== imageGenMode || _lastImageGenMode === null) {
    _lastImageGenMode = imageGenMode;
    _scheduleReflow();
  }
}
function _applyDesktopUI(enabled) {
  desktopEnabled = !!enabled;
  document
    .getElementById("desktopToggle")
    ?.classList.toggle("active", desktopEnabled);
  document
    .getElementById("desktopBadge")
    ?.classList.toggle("visible", desktopEnabled);
  /* The desktop agent now shares ONE toolbar entry with the browser bridge
   * (#localControlToggle). Its summary badge counts both capabilities, so it
   * is repainted here rather than toggled per-flag. */
  if (typeof _lcUpdateBadge === "function") _lcUpdateBadge();
}
function captureActiveConversationSettings() {
  const conv = getActiveConv();
  if (!conv) return;
  /* NEVER launder a paint-time default into stored truth. config.model may
   *   be a DISPLAY fallback (serverModel) chosen only because this conv had
   *   nothing stored; persisting it would overwrite the conversation's real
   *   model with the global default and make the next send run on the wrong
   *   model. Only a value the user actually chose (or one restored from the
   *   conv itself) is durable. A conv with no model simply keeps none — the
   *   composer still renders the default, it just stops being written down. */
  if (!config._modelIsProvisional && config.model) {
    conv.model = config.model;
    if (config.modelRef && typeof config.modelRef === 'object') {
      conv.modelRef = Object.assign({}, config.modelRef);
      conv.preferredProviderId = config.preferredProviderId || '';
    }
  }
  conv.thinkingDepth = config.thinkingDepth;
  /* FIX: Track the selected image gen model separately so pure image-gen
   * conversations accurately record which model was actually used, without
   * polluting the chat model field (which _applyModelUI reads on restore). */
  if (imageGenMode) {
    conv.imageGenModel = _igSelectedModel || 'gemini-3.1-flash-image-preview';
    conv.imageGenProviderId = _igSelectedProviderId || '';
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
  conv.autopilotEnabled = !!autopilotEnabled;
  conv.activeFlow = activeFlow || '';
  conv.imageGenEnabled = !!imageGenEnabled;
  conv.imageGenMode = !!imageGenMode;
  conv.humanGuidanceEnabled = !!humanGuidanceEnabled;
  conv.chatMode = chatMode || 'chat';
  conv.planMode = !!planMode;
  /* FIX: Sync projectPath from the UI-visible projectState to the conv object.
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
  /* Do not overwrite autoTranslate on a conversation with an active Turn.
   * The autoTranslate state is frozen at send-time. If the user toggles it OFF
   * while viewing this conversation, its running attempt must still use the
   * accepted command value rather than a later composer-global value. */
  const _taskActive = typeof convIsBusy === 'function' && convIsBusy(conv);
  if (!_taskActive) {
    conv.autoTranslate = !!autoTranslate;
  }
  /* FIX: Pass null instead of conv.id — toggling tools is a metadata-only
   * change, NOT new conversation activity.  Passing conv.id bumps
   * updatedAt = Date.now(), making the conversation jump to the top of the
   * sidebar just because the user toggled a tool button. */
  reconcileConversationCatalogMetadata(null);
  /* Settings persist independently of Turn content. Local-only shells refresh
   * their metadata cache; server-owned conversations use the unified settings
   * PATCH. Rapid composer changes coalesce into one write. */
  scheduleConversationSettingsPersist(conv);
}

// ── Debounced conversation-settings persistence ──
const _conversationSettingsPersistTimers = new Map();
function scheduleConversationSettingsPersist(conv, delayMs = 1500) {
  if (!conv?.id) return;
  const existing = _conversationSettingsPersistTimers.get(conv.id);
  if (existing) clearTimeout(existing);
  _conversationSettingsPersistTimers.set(conv.id, setTimeout(async () => {
    _conversationSettingsPersistTimers.delete(conv.id);
    try {
      const persisted = await persistConversationSettings(conv);
      if (!persisted) {
        console.warn(
          `[ConversationSettings] persist rejected for ${conv.id.slice(0, 8)}`,
        );
      }
    } catch (err) {
      console.warn(`[ConversationSettings] persist error: ${err.message}`);
    }
  }, delayMs));
}

function restoreConversationSettingsToComposer(conv) {
  /* Old/local snapshots may predate Plan Mode's exclusivity contract and carry
   * incompatible execution modes at the same time. Normalize the painted
   * runtime without writing during restore; the backend independently
   * fail-closes the accepted task config at its authority boundary. */
  const _workflowSelectionVisible = Boolean(
    typeof _featureFlags !== 'undefined' && _featureFlags.debug_mode);
  const _restoredInteraction = normalizeConversationInteractionModes(
    _workflowSelectionVisible ? conv : Object.assign({}, conv, {
      activeFlow: '',
    }));
  const _restoredPlanMode = _restoredInteraction.agentMode === 'plan';
  const _restoredWorkflowMode = _restoredInteraction.agentMode !== 'standard'
    || !!_restoredInteraction.activeFlow;
  config.thinkingDepth = conv.thinkingDepth || null;   // ← restore depth BEFORE model UI (let _applyModelUI normalize)
  config.modelRef = (conv.modelRef && typeof conv.modelRef === 'object')
    ? Object.assign({}, conv.modelRef) : null;
  config.preferredProviderId = conv.preferredProviderId || '';
  config.routing = config.preferredProviderId
    ? { preferred_provider_id: config.preferredProviderId } : {};
  /* Pass the conversation's OWN value only — no `|| serverModel` here. A conv
   * that stored nothing must reach _applyModelUI as falsy so the default it
   * paints is marked provisional and never written back. */
  _applyModelUI(conv.model || conv.preset || conv.effort);
  _applySearchModeUI(conv.searchMode || "multi");
  _applyFetchEnabledUI(true);  // always on
  _applyCodeExecUI(!!conv.codeExecEnabled);
  _applyBrowserUI(!!conv.browserEnabled);
  _applyDesktopUI(!!conv.desktopEnabled);
  _applyMemoryUI(conv.memoryEnabled !== undefined ? !!conv.memoryEnabled : true);
  _applyAgentModeUI(_restoredInteraction.agentMode);
  _applyFlowUI(_restoredInteraction.activeFlow);
  _applyImageGenToolUI(!!conv.imageGenEnabled);
  _applyImageGenUI(_restoredWorkflowMode ? false : !!conv.imageGenMode);
  _applyHumanGuidanceUI(_restoredPlanMode ? true : !!conv.humanGuidanceEnabled);
  /* Per-conv write-mode override; absent means the global default (Auto). */
  autoApplyWrites = conv.autoApply !== undefined ? !!conv.autoApply : true;
  _updateAutoApplyUI();
  /* Three-tier dial: use the stored tier, or derive it from the atomic
   * flags for a pre-feature conversation. Paint-only (no persist / no modal)
   * so restoring never clobbers the just-restored flags — _applyChatModeUI is
   * idempotent with the setters above. */
  if (typeof _applyChatModeUI === 'function') {
    const _storedMode = conv.chatMode
      || (typeof _deriveChatModeFromFlags === 'function' ? _deriveChatModeFromFlags(conv) : 'chat');
    /* Studio ⟺ a project is attached — the clamp is BIDIRECTIONAL.
     * (a) A stored 'studio' tier with NO projectPath is poisoned (e.g.
     * persisted before the project was cleared — the clear path once
     * repainted without saving) — never restore it.
     * (b) The mirror poison: a stored NON-studio tier WITH a projectPath
     * (stamped by the one-way projectState→conv sync while another conv's
     * project was globally active; the dial can never produce this on
     * purpose — picking Chat detaches the project). A project attached IS
     * Studio, so restore heals it upward. Both heals are paint-only, like
     * the original one-way clamp. */
    const _mode = (_storedMode === 'studio' && !conv.projectPath) ? 'chat'
      : (_storedMode !== 'studio' && conv.projectPath) ? 'studio'
      : _storedMode;
    _applyChatModeUI(_mode);
  }
  /* Restore the image gen model + batch count + aspect + resolution from conv settings */
  if (conv.imageGenModel) _igSelectedModel = conv.imageGenModel;
  _igSelectedProviderId = conv.imageGenProviderId || '';
  if (conv.imageGenCount) {
    _igSelectedCount = conv.imageGenCount;
    document.querySelectorAll('#igCountBar .ig-pill').forEach(b =>
      b.classList.toggle('active', parseInt(b.dataset.count) === _igSelectedCount));
    const genText = document.querySelector('.ig-gen-text');
    if (genText) genText.textContent = _igSelectedCount > 1 ? t('main.igBatchGo', { n: _igSelectedCount }) : t('main.igGenerate');
  }
  /* Restore aspect ratio selection */
  if (conv.imageGenAspect) {
    _igSelectedAspect = conv.imageGenAspect;
    document.querySelectorAll('#igAspectBar .ig-pill').forEach(b =>
      b.classList.toggle('active', b.dataset.ar === _igSelectedAspect));
  }
  /* Restore resolution selection */
  if (conv.imageGenResolution) {
    _igSelectedResolution = conv.imageGenResolution;
    document.querySelectorAll('#igResolutionBar .ig-pill').forEach(b =>
      b.classList.toggle('active', b.dataset.res === _igSelectedResolution));
  }
  /* Auto-translate toggle: restore the per-conv frozen value, BUT never pull
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
  if (typeof runtimeScope.updateContextBar === 'function') runtimeScope.updateContextBar();
  /* Re-filter the cross-conversation presence strip to this conversation's
   *   project root immediately; the signal-driven owner has no fallback poll. */
  if (typeof runtimeScope.presenceRefresh === 'function') runtimeScope.presenceRefresh();
  /* If the Project Brain panel is open, re-resolve its feed to the new
   *   conversation's project (two projects must never bleed into one view). */
  if (typeof runtimeScope.projectBrainRefresh === 'function') runtimeScope.projectBrainRefresh();
  /* Reflow after restoring composer state (toolbar width may differ). */
  _scheduleReflow();
}
function _resetToolsToDefaults() {
  config.thinkingDepth = config.defaultThinkingDepth;   // ← reset to default depth BEFORE applying model UI (let _applyModelUI normalize)
  /* A brand-new chat has stored nothing — pass nothing, so the default is
   * painted as provisional rather than immediately owned. */
  _applyModelUI();
  _applySearchModeUI("multi");
  _applyFetchEnabledUI(true);
  _applyCodeExecUI(false);
  _applyBrowserUI(false);
  /* Desktop was the ONE tool flag this function never reset, so a new chat
   * silently inherited computer control from whatever conversation came
   * before — `restoreConversationSettingsToComposer` sets it per conversation,
   * nothing cleared it, and
   * the merged badge then reported it as active on a conversation the user
   * never granted it on. It is the highest-risk flag here (shell, file writes,
   * GUI), so inheriting it by omission is the wrong default. */
  _applyDesktopUI(false);
  _applyMemoryUI(true);
  /* Execution strategy is independent from project attachment. Every new
   * conversation starts in Standard agent mode; attaching a project changes
   * only the capability tier and the project projection. */
  _applyAgentModeUI('standard');
  _applyFlowUI('');
  _applyImageGenToolUI(false);
  _applyImageGenUI(false);
  /* Human Guidance was never reset here, so a new chat silently inherited it
   * from whatever conversation came before — and since entering Plan mode
   * force-enables it (setAgentMode('plan') → _applyHumanGuidanceUI(true)),
   * any single Plan use left it ON for every subsequent new chat. Same
   * class of bug as the Desktop flag above: reset it to the default OFF. */
  _applyHumanGuidanceUI(false);
  /* New chat defaults to the Chat tier (everyday all-rounder). This runs the
   * derivation setters again, but they were just set to matching defaults
   * above, so it's a no-op paint + segmented-control highlight. */
  if (typeof _applyChatModeUI === 'function') _applyChatModeUI('chat');
  if (typeof paperMode !== 'undefined' && paperMode && typeof exitPaperMode === 'function') exitPaperMode();
  if (typeof exitResearchMode === 'function') exitResearchMode();
  _applyAutoTranslateUI(convAutoTranslate(null));
  /* Reset image gen creative mode settings to defaults */
  _igSelectedAspect = '1:1';
  _igSelectedResolution = '1K';
  _igSelectedCount = 1;
  _igSelectedProviderId = '';
  document.querySelectorAll('#igAspectBar .ig-pill').forEach(b =>
    b.classList.toggle('active', b.dataset.ar === '1:1'));
  document.querySelectorAll('#igResolutionBar .ig-pill').forEach(b =>
    b.classList.toggle('active', b.dataset.res === '1K'));
  document.querySelectorAll('#igCountBar .ig-pill').forEach(b =>
    b.classList.toggle('active', parseInt(b.dataset.count) === 1));
  const genText = document.querySelector('.ig-gen-text');
  if (genText) genText.textContent = t('toolbar.generate');
  if (typeof updateSubmenuCounts === 'function') updateSubmenuCounts();
  /* Reflow toolbar after resetting tools (toolbar width may differ). */
  _scheduleReflow();
}

/* ═══════════════════════════════════════════════════════════════════
   Functions originally defined here (newChat, loadConversation,
   sendMessage, initActiveTasks, ...) live in the
   `frontend/src/runtime/main/` subpackage. The bundler concatenates them BEFORE
   this file (see `Vite's module graph::the migrated module graph`), so by the time
   the boot IIFE below runs every symbol is in window scope.
   ═══════════════════════════════════════════════════════════════════ */


/**
 * WebView zero-height guard. On some Android WebViews the initial containing
 * block is measured as INDEFINITE height during/after load, so the base CSS
 * `html{height:100%}` / `body{height:100dvh}` both resolve to 0 — collapsing
 * the entire `.main → .chat-wrapper → #chatContainer` flex chain to height:0
 * and painting a blank page, even though `window.innerHeight` reports the true
 * viewport (observed: innerHeight=799 but html/body computed height=0). Desktop
 * Chrome resolves the percentage/vh correctly; the WebView does not.
 *
 * Fix: pin html/body height to the KNOWN-GOOD `window.innerHeight` in pixels,
 * sidestepping the broken %/dvh resolution. Kept in sync on resize/orientation.
 * A no-op on browsers where the chain is already non-zero (setting the same
 * pixel height the layout already has changes nothing visible). Runs at boot
 * and once more on the next frame to catch a late viewport establishment.
 *
 * Also publishes `--vh100` (window.innerHeight in px) on :root so any CSS that
 * sizes off the viewport (e.g. modal `max-height`) can consume a known-good
 * pixel height with a plain `vh` fallback — same WebView quirk collapses those
 * to ~0 too (observed: the software-update modal clipped to a sliver).
 */
function _installViewportHeightGuard() {
  function apply() {
    var h = window.innerHeight;
    if (!h || h < 1) return;               // don't pin to a bogus 0
    var px = h + 'px';
    // Use setProperty(...,'important') so this inline value beats stylesheet
    // rules marked !important (e.g. the phone block's
    // `body{height:100dvh!important}`, where 100dvh collapses to 0 in this
    // WebView). Inline !important outranks stylesheet !important.
    document.documentElement.style.setProperty('height', px, 'important');
    document.body.style.setProperty('height', px, 'important');
    document.documentElement.style.setProperty('--vh100', px);
  }
  apply();
  requestAnimationFrame(apply);            // re-apply after first layout frame
  window.addEventListener('resize', apply);
  window.addEventListener('orientationchange', function () {
    // orientation metrics settle a beat late; re-apply shortly after.
    setTimeout(apply, 50); setTimeout(apply, 300);
  });
}

/* Build watch — the long-lived-tab handshake. A tab keeps running the
 * bundle it was loaded with indefinitely: the vite:preloadError reload only
 * fires when a lazy chunk 404s, so a rebuilt frontend never reaches an old
 * tab that stays on already-loaded code (the "bug fixed hours ago but the
 * user still sees it" class — e.g. the sidebar 今天→昨天→今天 date-group
 * interleave, whose fix only reached tabs that happened to reload).
 * The existing push ping/pong carries the served entry-bundle basename on a
 * 5-minute / visibility-resume probe; hard-reload when it differs from ours.
 * This adds no HTTP poll, timer, or visibility listener. Reloads are
 * IDLE-GATED (no active streams, empty composer) so an
 * in-flight generation or a half-typed draft is never yanked, and
 * LOOP-GUARDED (one attempt per build id per tab session) so a stale-cached
 * index.html upstream can never spin the tab into a reload loop. The typed
 * build-watch controller owns bounded busy-defer and session-guard state. */

function _loadedBuildId() {
  /* The boot graph's own entry script tag carries the content hash:
   * <script type="module" src="static/vite/assets/main-<hash>.js">. */
  const el = document.querySelector(
    'script[src*="/static/vite/assets/main-"], script[src^="static/vite/assets/main-"]');
  const src = el ? (el.getAttribute('src') || '') : '';
  const m = src.match(/assets\/(main-[^/?#]+\.js)/);
  return m ? m[1] : null;
}

function _buildWatchBusy() {
  if (typeof conversations !== 'undefined' && Array.isArray(conversations) &&
      conversations.some(c => typeof convIsBusy === 'function' && convIsBusy(c))) return true;
  const ta = document.getElementById('userInput');
  return !!(ta && ta.value && ta.value.trim());
}

/* Test seam: the page-reload primitive, indirect so jsdom harnesses can spy
 * on navigation (jsdom's location.reload is unimplemented). */
function _reloadPage() { window.location.reload(); }

function _startBuildWatch() {
  if (typeof buildWatchController !== 'undefined') buildWatchController.start();
}

// ── Event bindings ──
(function init() {
  try {
  // WebView zero-height guard FIRST — before any layout-dependent init, so the
  // document has a real pixel height for the flex chain to fill.
  try { _installViewportHeightGuard(); } catch (_) {}
  /* Fail loudly if the sole TurnStore -> Surface composition seam is absent. */
  if (typeof runtimeScope.requestAuthoritativeConversationRender !== 'function') {
    const _surfaceBootMessage = '[ConversationSurface] MISSING at boot — ' +
      'the typed conversation composition graph is incomplete.';
    console.error(_surfaceBootMessage);
    try {
      const banner = document.createElement('div');
      banner.style.cssText = 'position:fixed;top:0;left:0;right:0;z-index:99999;' +
        'background:#7f1d1d;color:#fff;padding:10px 16px;' +
        'font:13px/1.4 monospace;text-align:center;';
      banner.textContent = _surfaceBootMessage;
      (document.body || document.documentElement).appendChild(banner);
    } catch (_) { /* the console error remains the fallback */ }
  }
  // ── Init model toggle from config ──
  (function initModelToggle() {
    thinkingEnabled = true;
    _applyModelUI(config.model);
    _loadServerConfigAndPopulate();
  })();
  // Apply stored input-send-mode hint text on load
  try { refreshInputSendHint(); } catch (_) {}
  /* Composer left-edge lock: mirror the rail grant onto .input-inner now
   * (first paint) and on every .chat-container resize (sidebar/drawer/
   * window). Without the boot call the composer renders one frame with the
   * no-rail defaults and then snaps. */
  try { _installRailGeometryMirror(); } catch (e) { console.warn('rail mirror install failed:', e); }
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
  if (typeof updateSubmenuCounts === "function") updateSubmenuCounts();
  renderConversationList();
  /* ROOT FIX (sidebar click-loss race): conversation activation happens on
   * pointerdown for mouse input, NOT on click. The sidebar legitimately
   * re-renders its rows in the background (stream finish bumps updatedAt →
   * resort + innerHTML rebuild, 60s server-poll merges, folder re-partition,
   * auto-title arrival, windowed page appends). A `click` event only fires
   * when the SAME DOM node receives both mousedown and mouseup, so any
   * rebuild landing mid-press silently destroys the click — the reported
   * "clicking a conversation sometimes does nothing at all" symptom
   * (reproduced deterministically: rebuild between mousedown/mouseup ⇒
   * no click event, no handler run, zero feedback). pointerdown fires at
   * press time on the row actually under the cursor, before any rebuild can
   * interfere. Touch/pen deliberately keep the click path so touch-scrolling
   * the list can never activate a conversation. */
  let _lastConvPointerActivation = { id: null, t: 0 };
  function _handleConvPointerDown(e) {
    if (e.pointerType !== "mouse" || e.button !== 0) return;
    /* Row action buttons (rename/delete/duplicate/folder/ref/copy) keep their
     * own click handlers below — pressing one must not also switch the
     * conversation out from under the action. */
    if (e.target.closest(".conv-actions, .conv-action-btn, button, a")) return;
    const item = e.target.closest(".conv-item");
    if (!item || !item.dataset.convId) return;
    _lastConvPointerActivation = { id: item.dataset.convId, t: Date.now() };
    loadConversation(item.dataset.convId);
  }
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
    // Duplicate conversation button
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
    // Rename conversation button — inline title edit dialog
    const rename = e.target.closest(".conv-rename");
    if (rename) {
      e.stopPropagation();
      if (rename.dataset.convId) _promptRenameConversation(rename.dataset.convId);
      return;
    }
    // @ reference button — add conversation reference chip
    const ref = e.target.closest(".conv-ref");
    if (ref) {
      e.stopPropagation();
      if (ref.dataset.convId) {
        addConvRef(ref.dataset.convId, ref.dataset.convTitle || "Untitled");
      }
      return;
    }
    // Folder assign button — show folder picker dropdown
    const folderAssign = e.target.closest(".conv-folder-assign");
    if (folderAssign) {
      e.stopPropagation();
      if (folderAssign.dataset.convId) _showFolderPicker(folderAssign.dataset.convId, folderAssign);
      return;
    }
    // Folder assign button — handled above
    const item = e.target.closest(".conv-item");
    if (item && item.dataset.convId) {
      /* The pointerdown handler already activated this row (mouse path) —
       * running loadConversation again on the click would double the
       * render work, refresh the authoritative snapshot, and refetch the
       * server queue for zero user-visible effect. Touch/pen/keyboard clicks
       * never set the token, so they still activate here. */
      const lp = _lastConvPointerActivation;
      if (lp.id === item.dataset.convId && Date.now() - lp.t < 1000) {
        _lastConvPointerActivation = { id: null, t: 0 };
        return;
      }
      loadConversation(item.dataset.convId);
    }
  }
  document
    .getElementById("convList")
    .addEventListener("pointerdown", _handleConvPointerDown);
  document
    .getElementById("convList")
    .addEventListener("click", _handleConvClick);
  // Initialize folder drag-and-drop
  _initFolderDragDrop();
  // ── Folder tab bar click + context menu ──
  _initFolderTabs();
  const ta = document.getElementById("userInput");
  /* PERF (INP): autosize forces a synchronous layout (set height=auto, read
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
        await _handleImageDrop(f);
      }
    }
    // Detect log noise in pasted text — server-side via /api/v1/logs/clean
    if (!hasImage) {
      const pastedText = e.clipboardData?.getData("text");
      if (pastedText && pastedText.length > 200) {
        setTimeout(async () => {
          const result = await runtimeScope.detectLogNoise(ta.value);
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
      // Project modal open → the folder browser owns file drops (save-to-disk).
      if (runtimeScope._tofuProjectModalOpen) return;
      // Knowledge base open → its dropzone owns file drops (corpus upload).
      if (runtimeScope._tofuKnowledgeModalOpen) return;
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
      // Project modal open → the folder browser's capture-phase handler saves
      // the file to disk and stopPropagation()s; if a drop lands OUTSIDE the
      // browser box we simply ignore it rather than attaching to chat.
      if (runtimeScope._tofuProjectModalOpen) return;
      // Knowledge base open → the dropzone's _knowledgeDrop handles on-target
      // drops; anything else is swallowed here. The global dragover already
      // allowed the drop, so returning WITHOUT preventDefault would let the
      // browser navigate to the dropped file (app state loss).
      if (runtimeScope._tofuKnowledgeModalOpen) {
        e.preventDefault();
        _dragCounter = 0;
        if (overlay) overlay.classList.remove("visible");
        return;
      }
      e.preventDefault();
      _dragCounter = 0;
      if (overlay) overlay.classList.remove("visible");
      const files = Array.from(e.dataTransfer?.files || []);

      // Paper mode: route PDFs into the paper reader instead of main input
      if (typeof paperMode !== 'undefined' && paperMode) {
        for (const f of files) {
          if (f.type === "application/pdf" || f.name.toLowerCase().endsWith(".pdf")) {
            if (typeof runtimeScope._handlePaperFileDrop === 'function') {
              await runtimeScope._handlePaperFileDrop(f);
              break; // Only one PDF at a time in paper reader
            }
          }
        }
        return;
      }

      // Edit mode uses the shared pendingImages/pendingPdfTexts — no separate handlers needed.
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
    if (e.target === document.getElementById("settingsModal")) runtimeScope.closeSettings();
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
          _updateScrollToBottomBtn();
          _scrollTicking = false;
        });
      }
    },
    { passive: true },
  );
  /* Keep the scroll-to-bottom button anchored just above the composer on
   * every viewport (desktop full toolbar ≈150px, mobile compact ≈70-110px,
   * grows with attachment previews / mode bars) — publish the live
   * .input-area height as a CSS var instead of guessing a fixed offset. */
  (function _trackInputAreaHeight() {
    const inputArea = document.querySelector(".input-area");
    const wrapper = document.querySelector(".chat-wrapper");
    if (!inputArea || !wrapper) return;
    const publish = () => {
      wrapper.style.setProperty("--input-area-h", inputArea.offsetHeight + "px");
      _updateScrollToBottomBtn();
    };
    publish();
    if (typeof ResizeObserver === "function") {
      try {
        new ResizeObserver(publish).observe(inputArea);
      } catch (err) {
        console.warn("input-area ResizeObserver failed:", err);
      }
    }
  })();
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
        runtimeScope.closeSettings();
        e.preventDefault();
        return;
      }
      const prm = document.getElementById("projectModal");
      if (prm && prm.classList.contains("open")) {
        runtimeScope.closeProjectModal();
        e.preventDefault();
        return;
      }
      const brm = document.getElementById("localControlModal");
      if (brm && brm.classList.contains("open")) {
        runtimeScope.closeLocalControlModal();
        e.preventDefault();
        return;
      }
      const cm = document.getElementById("dailyReportModal");
      if (cm && cm.classList.contains("open")) {
        runtimeScope.closeDailyReport();
        e.preventDefault();
        return;
      }
    }
  });
  initSidebarSearch();
  /* Voice input — probe backend capability + browser support; the mic button
     stays hidden unless a transcription model is configured. Fire-and-forget. */
  if (typeof runtimeScope.initVoiceInput === 'function') { runtimeScope.initVoiceInput(); }
  /* DB-first boot: conversations[] starts empty and is populated by
   *   loadConversationCatalog() inside initActiveTasks().
   *   The sidebar shows a brief loading indicator (~16ms) until the
   *   server responds.  This eliminates all localStorage desync bugs.
   *   On failure we fall back to the IndexedDB cache + a backoff retry
   *   (see _bootReconnectWithBackoff below). */
  /* Restore last active conversation from sessionStorage (if any).
   *   If the conv exists on the server, we'll navigate to it after loading.
   *   Otherwise, fall back to the most recent conversation. */
  /* sessionStorage is PER-TAB and dies on a browser close/reopen — it only
   *   survives an in-tab reload. Fall back to the localStorage mirror (written
   *   on every leave via _persistLastActiveConv below) so a genuine COLD open
   *   (new tab / browser restarted) still knows the last-active conversation,
   *   matching ChatGPT's restore-your-last-chat behaviour. */
  const _restoredConvId = sessionStorage.getItem('tofu_activeConvId')
    || (function () { try { return localStorage.getItem('tofu_lastActiveConvId'); } catch (_e) { return null; } })()
    || null;
  newChat();  /* show welcome screen immediately */
  {
    const convList = document.getElementById('conversationList');
    if (convList) convList.innerHTML = '<div style="text-align:center;padding:18px 0;color:#999;font-size:13px">Loading…</div>';
  }
  /* Backend liveness and Sidecar readiness are separate typed verdict owners.
   * Their overlapping startup health reads share one body-safe request flight;
   * neither owner can block catalog/folder initialization. */
  backendAvailabilityMonitor.start();
  void storageAvailabilityMonitor.check();

  // ── Restore PDF/VLM state from sessionStorage (survives page refresh) ──
  if (typeof _vlmRestoreState === 'function') {
    _vlmRestoreState().catch(e => console.warn('[VLM-Restore] Failed:', e));
  }

  /* Paint the sidebar from the IndexedDB cache IMMEDIATELY, before the
   *   server round-trip. On a flaky tunnel / mobile network the boot fetch
   *   can throw (`Failed to fetch`) and previously left a dead "Loading…"
   *   placeholder. Cache-first paint shows metadata-only catalog rows with
   *   zero network dependency; TurnStore hydrates the selected transcript
   *   from Conversation Sync v3. */
  if (typeof hydrateConversationCatalogFromCache === 'function') {
    hydrateConversationCatalogFromCache().then(() => {
      /* First-open seamlessness (ChatGPT-parity): the moment the cached
       *   sidebar is painted, open the user's LAST-ACTIVE conversation from the
       *   cache — BEFORE the catalog round-trip. loadConversation transfers
       *   Surface ownership immediately and starts authoritative hydration, so
       *   a returning user lands directly in their conversation instead of
       *   staring at the "New Chat" welcome for the whole boot fetch on a slow /
       *   flaky tunnel (the welcome→snap-to-conv flash). Gated to the EXACT
       *   restored id found in the cache — NEVER the conversations[0] fallback,
       *   which stays in the post-server _bootRestoreActiveConv where the list
       *   is authoritative — and only while still on the welcome screen
       *   (activeConvId null ⇒ the user hasn't navigated yet). The post-server
       *   _bootRestoreActiveConv then no-ops this branch (it early-returns on a
       *   set activeConvId), and TurnStore hydration is idempotent, so the later
       *   server reconcile is a surgical in-place
       *   refresh, not a re-open. */
      if (!activeConvId && typeof loadConversation === 'function') {
        let _target = null;
        if (_restoredConvId && conversations.find(c => c.id === _restoredConvId)) {
          /* Specific last-active conv IS in the cache → transfer its Surface
           *   immediately, then hydrate Turns. Covers in-tab reload AND a
           *   cold open whose localStorage mirror resolves in the cache. */
          _target = _restoredConvId;
        } else if (!_restoredConvId && conversations.length > 0) {
          /* COLD open with NO known id (first-ever visit's mirror empty, or
           *   storage cleared): open the MOST-RECENT cached conversation.
           *   hydrateConversationCatalogFromCache sorts with _convSorter (active-first,
           *   then updatedAt desc), so conversations[0] is the newest — the
           *   sidebar's top row. This kills the cold-open "New Chat welcome →
           *   snap to a conversation after the full round-trip" flicker too. */
          _target = conversations[0].id;
        }
        /* When _restoredConvId is set but NOT in the cache, we deliberately do
         *   NOTHING here: it can't be painted instantly anyway, and opening
         *   conversations[0] would strand the user on the wrong conv (the
         *   post-server _bootRestoreActiveConv opens the real id once the
         *   authoritative server list confirms it). */
        if (_target) loadConversation(_target);
      }
    }).catch(e => debugLog(`cache hydrate: ${e.message}`, 'warn'));
  }

  /* Cold-open restore mirror. sessionStorage.tofu_activeConvId is per-tab and
   *   dies on a browser quit, so mirror the active conv id into localStorage on
   *   every "leaving" signal — visibilitychange→hidden (the reliable one on
   *   mobile/tablet, where pagehide/beforeunload often don't fire) plus pagehide
   *   (desktop tab close). Boot reads this as the fallback restore id. Guarded
   *   on a non-null activeConvId so exiting on a blank new chat never clobbers
   *   the last REAL conversation. Best-effort; storage may be disabled. */
  const _persistLastActiveConv = () => {
    try { if (activeConvId) localStorage.setItem('tofu_lastActiveConvId', activeConvId); }
    catch (_e) { /* storage disabled — no-op */ }
  };
  window.addEventListener('pagehide', _persistLastActiveConv);
  document.addEventListener('visibilitychange', () => {
    if (document.visibilityState === 'hidden') _persistLastActiveConv();
  });

  /* Resolve this tab's storage owner before any push subscriber is wired.
   * Conversation and folder invalidations compare
   *   frame.userId against runtimeScope._currentUserId; a frame that arrives
   *   before this resolves is rejected. Personal installs receive ownerId=1
   *   from the same request principal boundary as every storage operation.
   *
   *   The enclosing boot scope is NOT async, so the ordering is expressed
   *   as a promise chain rather than `await`: the subscribers are wired in
   *   the .then(), i.e. strictly after the identity settles. initCurrentUserId
   *   never rejects (it swallows its own errors), but .catch() is kept so a
   *   future change there can never strand the subscribers unwired. A failed
   *   probe leaves push fail-closed and is retried on resume/reconnect. */
  const _identityReady = (typeof initCurrentUserId === 'function')
    ? initCurrentUserId().catch(() => {})
    : Promise.resolve();

  _identityReady.then(() => {
    /* Event-driven cross-device sync: subscribe to the server's `notify`
     *   push so a sibling device's change (new turn / rename / delete / folder)
     *   reconciles in real time. */
    if (typeof _wireConvSyncPush === 'function') _wireConvSyncPush();

    /* Long-lived-tab handshake (see _startBuildWatch above): reload the
     * tab when a push pong reports a NEWER frontend build than the bundle
     * this page is running — the root fix for "already-fixed sidebar bugs
     * (今天→昨天→今天) still visible in a days-old tab". Idle-gated and
     * loop-guarded; inert when the server predates pong buildId. */
    if (typeof _startBuildWatch === 'function') _startBuildWatch();
  });

  /* Bottom-follow suspension latch (core.js): arm the reader's
   * scroll-up-intent listeners as soon as the chat container exists, so a
   * wheel-up / touch-drag / scrollbar move away from the bottom detaches the
   * streaming auto-follow from the first gesture — no fighting, no tremble. */
  if (typeof _armFollowSuspensionListeners === 'function') _armFollowSuspensionListeners();

  initActiveTasks().then(() => {
    /* Decide reconnect by OBSERVABLE OUTCOME, not by a thrown error.
     *   loadConversationCatalog swallows Failed to fetch (try/catch →
     *   debugLog) and RESOLVES, so the .catch below never fires on the tunnel
     *   drop it targets. serverLoadOk() is the truth: false on throw / !resp.ok
     *   (→ reconnect), true on a real 200-with-data OR a legitimate 304. */
    const _ok = (typeof serverLoadOk === 'function') ? serverLoadOk() : true;
    if (!_ok) {
      debugLog('[boot] server load did not succeed — starting reconnect backoff', 'warn');
      _bootReconnectWithBackoff();
    } else {
      _clearBootReconnectBanner();
    }
    renderConversationList();
    _bootRestoreActiveConv(_restoredConvId);
    // After task reconnection, resume any pending translation tasks for active conv
    if (activeConvId) _resumePendingTranslations(activeConvId);
  }).catch(e => {
    debugLog(`Boot load failed: ${e.message}`, 'warn');
    /* Even if server load fails, the app is still usable — user can create new
     *   chats, and cached conversations are already painted. Show a visible,
     *   non-blocking "reconnecting" state and retry with backoff. */
    renderConversationList();
    _bootReconnectWithBackoff();
  });
  if (typeof _initSelectionPopup === "function") _initSelectionPopup();
  loadProjectStatus();
  _updateAutoApplyUI();
  _applyAutoTranslateUI();
  /* Conversation Sync v3 owns resume and periodic revalidation in
   * core/conversation_invalidation.js. */
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
    console.error('[main.js] ❌ Init crashed:',
      _initErr && (_initErr.stack || _initErr.message) || String(_initErr));
  }
  // Signal to loading-guard stubs that all scripts have loaded (MUST run even on error)
  if (typeof _markScriptsLoaded === 'function') _markScriptsLoaded();

  /* ── Boot reconnect: visible non-blocking banner + guarded backoff retry ──
   *
   * When the initial conversation load fails (common through a flaky VS Code
   * port-forward tunnel on mobile), the sidebar has already been painted from
   * the IndexedDB cache. We show a small, dismissible banner explaining that
   * we're showing CACHED conversations and reconnecting — NOT that the list is
   * complete — then retry loadConversationCatalog() with exponential
   * backoff. On success the id-keyed merge reconciles the cached shells and
   * the banner clears.
   *
   * Concurrency: _bootLoadInFlight guards against stacking with the 60s
   * refresh timer / cross-tab triggers so a flaky tunnel can't spawn a pile of
   * overlapping fetches. */
  /* Restore the last active conversation from before a page refresh — but
   *   ONLY when the user hasn't already navigated during this (possibly slow)
   *   server load. The sidebar is painted from the IndexedDB cache instantly,
   *   so on a poor connection the user can click into a conversation (or start
   *   a new-chat send) BEFORE the boot load resolves. newChat() reset
   *   activeConvId=null at boot and nothing but a user action sets it during
   *   the load, so a non-null activeConvId here means "the user already chose
   *   what to view" — restoring/auto-selecting now would yank them off it (the
   *   "loading finished and suddenly switched my conversation" bug) and, for a
   *   _turnSnapshotRequired target, flash the loading skeleton at the top. Only act when
   *   they're still on the welcome screen (activeConvId still null). */
  function _bootRestoreActiveConv(restoredId) {
    if (activeConvId) return;  // user already navigated during the load — leave them be
    const restoredConv = restoredId && conversations.find(c => c.id === restoredId);
    if (restoredConv) {
      loadConversation(restoredId);
      return;
    }
    if (conversations.length > 0) {
      const input = document.getElementById('messageInput');
      const hasInput = input && input.value.trim().length > 0;
      if (!hasInput) loadConversation(conversations[0].id);
    }
  }

  function _showBootReconnectBanner() {
    if (document.getElementById('boot-reconnect-banner')) return;
    const banner = document.createElement('div');
    banner.id = 'boot-reconnect-banner';
    /* If the storage warning (z-index 10000, top:0) is already showing,
     *   offset below it so the two STACK instead of overlapping at top:0 (the
     *   9999 boot banner would otherwise be hidden behind it). */
    const _storageBanner = document.getElementById('storage-warning-banner');
    const _topOffset = _storageBanner ? (_storageBanner.offsetHeight || 44) : 0;
    banner.style.cssText =
      'position:fixed;top:' + _topOffset + 'px;left:0;right:0;z-index:9999;' +
      'background:#b45309;color:#fff;padding:8px 14px;font-size:13px;' +
      'text-align:center;box-shadow:0 2px 8px rgba(0,0,0,.3);' +
      'display:flex;align-items:center;justify-content:center;gap:8px;';
    /* SVG glyph per CLAUDE.md §3.4 — no emoji for UI status. */
    const _bannerText = (typeof t === 'function')
      ? t('conn.bootReconnect')
      : '离线，显示缓存的对话，正在重连…';
    banner.innerHTML =
      '<span style="display:inline-flex"><svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M21 12a9 9 0 1 1-6.219-8.56"/></svg></span>' +
      '<span>' + _bannerText + '</span>';
    document.body.prepend(banner);
  }

  function _clearBootReconnectBanner() {
    const b = document.getElementById('boot-reconnect-banner');
    if (b) b.remove();
  }

  async function _bootReconnectWithBackoff() {
    /* Idempotent: the boot promise's .then(!ok) and .catch both call this, and
     *   though a promise settles once (so a single boot can't double-fire), we
     *   don't want to depend on that invariant — a second entry (future caller,
     *   re-boot path) must NOT spawn a concurrent backoff loop / second banner.
     *   window-scoped so it survives even if this IIFE is re-evaluated. */
    if (runtimeScope._bootReconnectStarted) {
      debugLog('[boot-reconnect] already running — not starting a second loop', 'warn');
      return;
    }
    runtimeScope._bootReconnectStarted = true;
    _showBootReconnectBanner();
    const _delays = [2000, 4000, 8000, 15000, 30000];
    try {
      for (let i = 0; i < _delays.length; i++) {
        await new Promise(r => setTimeout(r, _delays[i]));
        /* Acquire the shared self-healing in-flight lease. `_acquireBootLoad`
         *   is a no-op-returns-false when another load holds a FRESH lease, but
         *   reclaims a STALE one (a prior load that never settled through the
         *   tunnel) so a wedged load can't disable boot reconnect forever.
         *   Fall back to the bare latch only if the invalidation owner failed
         *   to initialize before main.js. */
        const _acq = (typeof runtimeScope._acquireBootLoad === 'function')
          ? runtimeScope._acquireBootLoad
          : () => { if (runtimeScope._bootLoadInFlight) return false; runtimeScope._bootLoadInFlight = Date.now(); return true; };
        const _rel = (typeof runtimeScope._releaseBootLoad === 'function')
          ? runtimeScope._releaseBootLoad
          : () => { runtimeScope._bootLoadInFlight = 0; };
        if (!_acq()) continue;  // another load (timer/cross-tab) holds a fresh lease
        try {
          await loadConversationCatalog();
          /* The call swallows errors + resolves, so success is the observable
           *   flag, not the absence of a throw. */
          if (typeof serverLoadOk !== 'function' || serverLoadOk()) {
            _clearBootReconnectBanner();
            /* Converge folders immediately when the catalog reconnects instead
             *   of waiting for the folder owner's bounded retry timer. The
             *   single-flight loader shares this request with any retry/push
             *   refresh already in progress. */
            if (typeof loadFolders === 'function' &&
                typeof areFoldersLoaded === 'function' && !areFoldersLoaded()) {
              await Promise.resolve(loadFolders()).catch(e =>
                debugLog(`[boot-reconnect] folder reload failed: ${e && e.message}`, 'warn'));
            }
            renderConversationList();
            debugLog(`[boot-reconnect] recovered on attempt ${i + 1}`, 'success');
            return;
          }
          debugLog(`[boot-reconnect] attempt ${i + 1}: still not reachable`, 'warn');
        } catch (e) {
          debugLog(`[boot-reconnect] attempt ${i + 1} threw: ${e.message}`, 'warn');
        } finally {
          _rel();
        }
      }
      debugLog('[boot-reconnect] gave up after backoff; 60s timer will keep trying', 'warn');
    } finally {
      /* Release the idempotency latch so a LATER genuine failure (or the 60s
       *   timer detecting a fresh drop) can restart the backoff loop. */
      runtimeScope._bootReconnectStarted = false;
    }
  }
})();
