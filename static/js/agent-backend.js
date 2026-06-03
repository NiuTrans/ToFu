/* agent-backend.js — Agent Backend Selection (Builtin / Claude Code / Codex)
   Extracted from main.js. Concatenated by lib/js_bundler.py BEFORE main.js
   so the let-declared module vars + functions are visible to main.js. */

/* ═══════════════════════════════════════════
   Agent Backend Selection
   ═══════════════════════════════════════════ */
let activeAgentBackend = 'builtin';       // 'builtin' | 'claude-code' | 'codex'
let _agentBackendCapabilities = null;     // BackendCapabilities from server
let _agentBackendCache = null;            // Cached /api/v1/agent-backends/status result

/**
 * Fetch backend status from server and cache it.
 * @returns {Promise<Array>} List of backend info objects.
 */
async function _fetchAgentBackends() {
  try {
    const data = await Api.agentBackends.status();
    _agentBackendCache = (data && data.backends) || [];
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
