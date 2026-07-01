/* ═══════════════════════════════════════════════════════════════════
   main toolbar ui — extracted from main.js (split 2026-05-28)

   Toolbar UI: model dropdown, presets, auto-translate, submenu, browser/endpoint/autopilot toggles.

   This file is concatenated by lib/js_bundler.py BEFORE main.js so
   the boot IIFE can reference these symbols. Symbols share `window`
   scope — no imports / exports needed.
   ═══════════════════════════════════════════════════════════════════ */

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

  // AI enhance: codeExec, memory, translate
  const aiCount = (codeExecEnabled ? 1 : 0) + (memoryEnabled ? 1 : 0) + (autoTranslate ? 1 : 0);
  _setCount(document.getElementById("submenuAICount"), aiCount);
  const aiTrigger = document.querySelector("#submenuAI .submenu-trigger");
  if (aiTrigger) aiTrigger.classList.toggle("has-active", aiCount > 0);

  // Tools: browser, desktop, scheduler, image gen, human guidance
  const toolCount = (browserEnabled ? 1 : 0) + (desktopEnabled ? 1 : 0) + (schedulerEnabled ? 1 : 0) + (imageGenEnabled ? 1 : 0) + (humanGuidanceEnabled ? 1 : 0);
  _setCount(document.getElementById("submenuToolsCount"), toolCount);
  const toolTrigger = document.querySelector("#submenuTools .submenu-trigger");
  if (toolTrigger) toolTrigger.classList.toggle("has-active", toolCount > 0);

  // Mode: swarm, endpoint, autopilot, flow
  const modeCount = (swarmEnabled ? 1 : 0) + (endpointEnabled ? 1 : 0) + (autopilotEnabled ? 1 : 0) + (activeFlow ? 1 : 0);
  _setCount(document.getElementById("submenuModeCount"), modeCount);
  const modeTrigger = document.querySelector("#submenuMode .submenu-trigger");
  if (modeTrigger) modeTrigger.classList.toggle("has-active", modeCount > 0);

  /* A pill appeared/disappeared → toolbar's intrinsic width shifted by the
   * pill's box.  Re-measure so .ps-label gets its space back. */
  if (widthChanged && typeof _scheduleReflow === "function") _scheduleReflow();
}

function cycleSearchMode() {
  const modes = ["off", "multi"];
  const idx = modes.indexOf(searchMode === "single" ? "multi" : searchMode);
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
    const d = await Api.browser.status();
    _applyBrowserLocalShortcut(d && d.extensionPath);
    _applyBrowserLnaWarning(d && d.chromeMajor);
    if (d && d.connected) {
      dot?.classList.replace("disconnected", "connected") ||
        dot?.classList.add("connected");
      dot?.classList.remove("disconnected");
      /* ★ Per-client routing: capture the first connected client's ID.
       * This ID is sent with every task so commands are routed to the
       * correct device's extension, not a random one. */
      const clients = d.clients || [];
      const clientCount = clients.length;
      /* secondsAgo is null until the first poll lands — render a fallback
       * instead of the literal string "nulls ago". */
      const ago = (d.secondsAgo != null) ? `${d.secondsAgo}s ago` : "just now";
      if (clientCount > 0) {
        /* Use the first connected client (most recently active) */
        const activeClient = clients[0];
        window._browserClientId = activeClient.client_id;
        const shortId = activeClient.client_id.substring(0, 8);
        txt &&
          (txt.textContent = clientCount > 1
            ? `${clientCount} extensions connected (using ${shortId}…)`
            : `Extension connected (${shortId}…, ${ago})`);
      } else {
        txt &&
          (txt.textContent = `Extension connected (${ago})`);
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

/* ★ When Tofu runs on the user's own machine the unpacked extension already
 * sits on disk — show its absolute path so they can "Load unpacked" it
 * directly with NO download/unzip. The path is click-to-copy. */
function _applyBrowserLocalShortcut(extPath) {
  const box = document.getElementById("browserLocalShortcut");
  const code = document.getElementById("browserExtPath");
  if (!box || !code) return;
  if (!extPath) {
    box.style.display = "none";
    return;
  }
  box.style.display = "";
  code.textContent = extPath;
  code.onclick = function () {
    if (typeof _safeClipboardWrite === "function") {
      _safeClipboardWrite(extPath)
        .then(() => code.classList.add("copied"))
        .catch(() => {});
    }
  };
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
  activeFlow = flowVal || '';
  const btn = document.getElementById("flowToggle");
  if (btn) btn.classList.toggle("active", !!activeFlow);
  const badge = document.getElementById("flowBadge");
  if (badge) badge.style.display = activeFlow ? "" : "none";
  const label = document.getElementById("flowActiveLabel");
  if (label) label.textContent = _flowDisplayName(activeFlow);
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
    + '<span class="flow-menu-check">✓</span>'
    + '<span class="flow-menu-text"><span class="flow-menu-name">' + escapeHtml(it.name) + '</span>'
    + '<span class="flow-menu-desc">' + escapeHtml(it.desc) + '</span></span>'
    + '</div>'
  ).join('');
}

// Close the flow menu on outside click.
document.addEventListener("click", (e) => {
  if (!e.target.closest("#flowMenuWrapper")) {
    const menu = document.getElementById("flowMenu");
    if (menu) menu.classList.remove("open");
  }
});

/* ═══ Folder management UI ═══ */

