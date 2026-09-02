"""Mini HTTP status server (SSE progress page + API-config form).

STDLIB-ONLY CONTRACT — see bootstrap_pkg.env_reexec.
"""
from __future__ import annotations

import http.server
import json
import os
import queue
import socket
import sys
import threading
import time

from . import providers, runtime
from .env_reexec import BASE_DIR

_STATUS_HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Tofu — Starting…</title>
<style>
  :root {
    --bg: #1a1b26; --surface: #24283b; --border: #414868;
    --text: #c0caf5; --text-dim: #565f89; --accent: #7aa2f7;
    --green: #9ece6a; --red: #f7768e; --yellow: #e0af68;
    --font: 'SF Mono', 'Fira Code', 'Cascadia Code', 'Consolas', monospace;
  }
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body {
    background: var(--bg); color: var(--text); font-family: var(--font);
    min-height: 100vh; display: flex; flex-direction: column;
    align-items: center; padding: 40px 20px;
  }
  h1 { font-size: 1.6rem; margin-bottom: 8px; color: var(--accent); }
  .subtitle { color: var(--text-dim); font-size: 0.85rem; margin-bottom: 32px; }

  /* ── Timeline ── */
  .timeline { width: 100%; max-width: 720px; margin-bottom: 24px; }
  .step {
    display: flex; align-items: flex-start; gap: 14px;
    padding: 12px 0; border-left: 2px solid var(--border);
    margin-left: 11px; padding-left: 20px; position: relative;
    transition: opacity 0.3s;
  }
  .step::before {
    content: ''; position: absolute; left: -7px; top: 16px;
    width: 12px; height: 12px; border-radius: 50%;
    background: var(--border); border: 2px solid var(--bg);
    transition: background 0.3s;
  }
  .step.active::before { background: var(--accent); box-shadow: 0 0 8px var(--accent); }
  .step.done::before   { background: var(--green); }
  .step.error::before  { background: var(--red); }
  .step-label { font-size: 0.9rem; font-weight: 600; }
  .step-detail { font-size: 0.78rem; color: var(--text-dim); margin-top: 4px; word-break: break-word; }

  /* ── Log panel ── */
  .log-panel {
    width: 100%; max-width: 720px; background: var(--surface);
    border: 1px solid var(--border); border-radius: 8px;
    padding: 16px; max-height: 45vh; overflow-y: auto;
    font-size: 0.76rem; line-height: 1.6;
    white-space: pre-wrap; word-break: break-all;
  }
  .log-panel .pip  { color: var(--yellow); }
  .log-panel .info { color: var(--text-dim); }
  .log-panel .err  { color: var(--red); }
  .log-panel .ok   { color: var(--green); }

  /* ── Status badge ── */
  .badge {
    display: inline-block; padding: 4px 14px; border-radius: 20px;
    font-size: 0.8rem; font-weight: 600; margin-bottom: 16px;
  }
  .badge.running  { background: rgba(122,162,247,0.15); color: var(--accent); }
  .badge.success  { background: rgba(158,206,106,0.15); color: var(--green); }
  .badge.failed   { background: rgba(247,118,142,0.15); color: var(--red); }

  /* ── Spinner ── */
  @keyframes spin { to { transform: rotate(360deg); } }
  .spinner {
    display: inline-block; width: 14px; height: 14px;
    border: 2px solid var(--border); border-top-color: var(--accent);
    border-radius: 50%; animation: spin 0.8s linear infinite;
    vertical-align: middle; margin-right: 6px;
  }

  /* ── Round counter ── */
  .round-info {
    font-size: 0.82rem; color: var(--text-dim); margin-bottom: 16px;
  }

  /* ── API Config Form (modal overlay) ── */
  .api-config-overlay {
    display: none; position: fixed; top: 0; left: 0; right: 0; bottom: 0;
    background: rgba(0,0,0,0.65); z-index: 1000;
    align-items: center; justify-content: center; padding: 20px;
  }
  .api-config-overlay.visible { display: flex; animation: fadeOverlay 0.3s ease; }
  @keyframes fadeOverlay { from { opacity: 0; } to { opacity: 1; } }
  .api-config-panel {
    background: var(--surface); border: 1px solid var(--accent);
    border-radius: 12px; padding: 28px; width: 100%; max-width: 520px;
    max-height: 85vh; overflow-y: auto;
    animation: slideUp 0.3s ease;
    box-shadow: 0 20px 60px rgba(0,0,0,0.5);
  }
  @keyframes slideUp { from { transform: translateY(20px); opacity: 0; } to { transform: none; opacity: 1; } }
  @keyframes fadeIn { from { opacity: 0; transform: translateY(8px); } to { opacity: 1; transform: none; } }
  .api-config-panel h2 {
    font-size: 1.1rem; color: var(--accent); margin-bottom: 6px;
  }
  .api-config-panel .hint {
    font-size: 0.78rem; color: var(--text-dim); margin-bottom: 18px; line-height: 1.5;
  }
  .api-config-panel label {
    display: block; font-size: 0.82rem; color: var(--text-dim);
    margin-bottom: 4px; margin-top: 12px;
  }
  .api-config-panel input, .api-config-panel select {
    width: 100%; padding: 8px 12px; font-size: 0.85rem;
    background: var(--bg); border: 1px solid var(--border);
    border-radius: 6px; color: var(--text); font-family: var(--font);
    outline: none; transition: border-color 0.2s;
  }
  .api-config-panel input:focus { border-color: var(--accent); }
  .api-config-panel .btn-row {
    display: flex; gap: 10px; margin-top: 20px;
  }
  .api-config-panel button {
    padding: 8px 20px; border: none; border-radius: 6px;
    font-family: var(--font); font-size: 0.85rem; font-weight: 600;
    cursor: pointer; transition: opacity 0.2s;
  }
  .api-config-panel button:hover { opacity: 0.85; }
  .api-config-panel .btn-primary {
    background: var(--accent); color: var(--bg);
  }
  .api-config-panel .btn-secondary {
    background: var(--border); color: var(--text);
  }
  .api-config-panel .status-msg {
    font-size: 0.8rem; margin-top: 10px; min-height: 1.2em;
  }
  .api-config-panel .status-msg.ok { color: var(--green); }
  .api-config-panel .status-msg.err { color: var(--red); }

  /* ── Provider template cards ── */
  .provider-templates {
    display: flex; flex-wrap: wrap; gap: 8px; margin-bottom: 12px;
  }
  .provider-tpl {
    padding: 5px 12px; border-radius: 6px; font-size: 0.78rem;
    background: var(--bg); border: 1px solid var(--border);
    color: var(--text-dim); cursor: pointer; transition: all 0.2s;
  }
  .provider-tpl:hover, .provider-tpl.active {
    border-color: var(--accent); color: var(--accent);
  }
</style>
</head>
<body>
  <h1>🔧 Tofu — Dependency Repair</h1>
  <p class="subtitle">Automatically installing missing packages…</p>
  <div id="badge" class="badge running"><span class="spinner"></span> Working…</div>
  <div id="round-info" class="round-info"></div>

  <div class="timeline" id="timeline"></div>

  <div class="log-panel" id="log"></div>

  <!-- API Config Form — modal popup shown on error -->
  <div class="api-config-overlay" id="apiConfigOverlay">
  <div class="api-config-panel">
    <h2>🔑 Configure API Access</h2>
    <p class="hint">
      Tofu needs an LLM API key to function. Pick a provider, choose a model,
      and paste your API key — the values will be saved to your
      <code>.env</code> and the server will restart.
    </p>

    <label>Provider</label>
    <div class="provider-templates" id="providerTemplates">
      <span class="provider-tpl" style="opacity:0.6">Loading…</span>
    </div>

    <label for="cfgModel">Model</label>
    <select id="cfgModel" style="margin-bottom:6px;"></select>
    <div style="display:flex; align-items:center; gap:6px; margin-bottom:6px;">
      <input type="text" id="cfgModelCustom" placeholder="…or type a custom model id"
             style="flex:1;">
    </div>

    <label for="cfgBaseUrl">Base URL</label>
    <input type="text" id="cfgBaseUrl" placeholder="https://api.openai.com/v1">

    <label for="cfgApiKey">API Key <span style="color:var(--red)">*</span></label>
    <input type="password" id="cfgApiKey" placeholder="sk-…" autocomplete="off">

    <div class="btn-row">
      <button class="btn-primary" onclick="_saveApiConfig()">💾 Save & Restart</button>
    </div>
    <div class="status-msg" id="cfgStatus"></div>
    <div style="text-align:center; margin-top:14px;">
      <a href="#" onclick="document.getElementById('apiConfigOverlay').classList.remove('visible'); return false;"
         style="color:var(--text-dim); font-size:0.78rem; text-decoration:none;">
        View error logs ↓
      </a>
    </div>
  </div>
  </div>

<script>
const timeline = document.getElementById('timeline');
const log = document.getElementById('log');
const badge = document.getElementById('badge');
const roundInfo = document.getElementById('round-info');

function addStep(id, label, cls) {
  let el = document.getElementById('step-' + id);
  if (!el) {
    el = document.createElement('div');
    el.className = 'step ' + (cls || '');
    el.id = 'step-' + id;
    el.innerHTML = '<div><div class="step-label"></div><div class="step-detail"></div></div>';
    timeline.appendChild(el);
  }
  el.querySelector('.step-label').textContent = label;
  if (cls) { el.className = 'step ' + cls; }
  return el;
}
function setStepDetail(id, detail) {
  const el = document.getElementById('step-' + id);
  if (el) el.querySelector('.step-detail').textContent = detail;
}

function appendLog(text, cls) {
  const span = document.createElement('span');
  span.className = cls || 'info';
  span.textContent = text + '\n';
  log.appendChild(span);
  log.scrollTop = log.scrollHeight;
}

// ── Provider templates (fetched from /bootstrap/provider-templates,
//    which merges bootstrap's builtin list with static/provider_templates/*.json) ──
let _providerTemplates = [];
let _currentTplKey = '';

async function _loadProviderTemplates() {
  try {
    const r = await fetch('/bootstrap/provider-templates',
                          { signal: AbortSignal.timeout(5000) });
    if (!r.ok) throw new Error('HTTP ' + r.status);
    _providerTemplates = await r.json();
  } catch (e) {
    // Fallback minimal list (should never be hit — the endpoint has a
    // hardcoded builtin list in the Python side).
    _providerTemplates = [
      { key: 'openai', name: 'OpenAI', base_url: 'https://api.openai.com/v1',
        protocol: 'responses', responses_profile: 'openai',
        models: [{ model_id: 'gpt-5.6-luna' }] },
      { key: 'custom',   name: 'Custom',   base_url: '',
        models: [{ model_id: '' }] },
    ];
  }
  _renderProviderTemplates();
}

function _renderProviderTemplates() {
  const container = document.getElementById('providerTemplates');
  container.innerHTML = '';
  _providerTemplates.forEach(function(tpl) {
    const span = document.createElement('span');
    span.className = 'provider-tpl';
    span.textContent = tpl.name || tpl.key;
    span.onclick = function() { _selectTemplate(tpl.key); };
    span.dataset.key = tpl.key;
    container.appendChild(span);
  });
  // Pick a sensible default
  const prefer = ['openai', 'anthropic', 'deepseek', 'openrouter', 'custom'];
  let defaultKey = '';
  for (const k of prefer) {
    if (_providerTemplates.find(t => t.key === k)) { defaultKey = k; break; }
  }
  if (!defaultKey && _providerTemplates.length) defaultKey = _providerTemplates[0].key;
  if (defaultKey) _selectTemplate(defaultKey);
}

function _selectTemplate(key) {
  const tpl = _providerTemplates.find(t => t.key === key);
  if (!tpl) return;
  _currentTplKey = key;
  document.getElementById('cfgBaseUrl').value = tpl.base_url || '';
  // Populate model dropdown
  const sel = document.getElementById('cfgModel');
  sel.innerHTML = '';
  const models = (tpl.models || []).filter(m => m && m.model_id);
  models.forEach(function(m) {
    const opt = document.createElement('option');
    opt.value = m.model_id;
    // Show capability hints where useful
    const caps = Array.isArray(m.capabilities) ? m.capabilities : [];
    const flags = [];
    if (caps.indexOf('thinking') >= 0) flags.push('🧠');
    if (caps.indexOf('vision')   >= 0) flags.push('👁');
    if (caps.indexOf('cheap')    >= 0) flags.push('💰');
    opt.textContent = m.model_id + (flags.length ? '  ' + flags.join('') : '');
    sel.appendChild(opt);
  });
  // Default to a cheap text model when available (users rarely want vision /
  // image-gen for bootstrap diagnosis; a cheap model keeps diagnosis cost tiny).
  let defaultIdx = 0;
  for (let i = 0; i < models.length; i++) {
    const caps = models[i].capabilities || [];
    if (caps.indexOf('cheap') >= 0 && caps.indexOf('image_gen') < 0 &&
        caps.indexOf('embedding') < 0) { defaultIdx = i; break; }
  }
  if (sel.options.length > 0) sel.selectedIndex = defaultIdx;
  document.getElementById('cfgModelCustom').value = '';
  // Highlight active card
  document.querySelectorAll('#providerTemplates .provider-tpl').forEach(el => {
    el.classList.toggle('active', el.dataset.key === key);
  });
}

function _showApiConfig() {
  document.getElementById('apiConfigOverlay').classList.add('visible');
  // Lazy-load templates on first open
  if (_providerTemplates.length === 0) _loadProviderTemplates();
  // Auto-focus the API key field
  setTimeout(() => document.getElementById('cfgApiKey').focus(), 300);
}
function _saveApiConfig() {
  const key = document.getElementById('cfgApiKey').value.trim();
  const url = document.getElementById('cfgBaseUrl').value.trim();
  const customModel = document.getElementById('cfgModelCustom').value.trim();
  const dropdownModel = document.getElementById('cfgModel').value.trim();
  const model = customModel || dropdownModel;
  const status = document.getElementById('cfgStatus');
  if (!key) {
    status.textContent = '❌ API Key is required';
    status.className = 'status-msg err';
    return;
  }
  status.textContent = '⏳ Saving…';
  status.className = 'status-msg';
  fetch('/bootstrap/save-config', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      api_key: key,
      base_url: url,
      model: model,
      custom_model: Boolean(customModel)
    })
  }).then(r => r.json()).then(d => {
    if (d.ok) {
      const picked = d.model && d.model !== model ? ' Using ' + d.model + '.' : '';
      status.textContent = '✅ Saved!' + picked + ' Restarting server…';
      status.className = 'status-msg ok';
      badge.className = 'badge running';
      badge.innerHTML = '<span class="spinner"></span> Restarting…';
      // Poll for server restart
      setTimeout(() => {
        const poll = setInterval(() => {
          fetch('/', { signal: AbortSignal.timeout(2000) }).then(r => {
            if (r.ok) { clearInterval(poll); window.location.href = '/?setup=1'; }
          }).catch(() => {});
        }, 2000);
      }, 2000);
    } else {
      status.textContent = '❌ ' + (d.error || 'Save failed');
      status.className = 'status-msg err';
    }
  }).catch(e => {
    status.textContent = '❌ Network error: ' + e.message;
    status.className = 'status-msg err';
  });
}

const es = new EventSource('/bootstrap/events');

es.addEventListener('phase', e => {
  const d = JSON.parse(e.data);
  addStep(d.id, d.label, d.status);
  if (d.detail) setStepDetail(d.id, d.detail);
  // Track handoff state — server.py is about to start
  if (d.id === 'handoff' || (d.id && d.id.startsWith('handoff-'))) {
    _handingOff = true;
  }
});

es.addEventListener('round', e => {
  const d = JSON.parse(e.data);
  roundInfo.textContent = 'Round ' + d.current + ' / ' + d.max;
});

es.addEventListener('log', e => {
  appendLog(e.data, 'info');
});

es.addEventListener('pip_output', e => {
  appendLog(e.data, 'pip');
});

es.addEventListener('error_text', e => {
  appendLog(e.data, 'err');
});

es.addEventListener('diagnosis', e => {
  const d = JSON.parse(e.data);
  addStep('diag', '🔍 Diagnosis', 'done');
  setStepDetail('diag', d.diagnosis);
  if (d.packages && d.packages.length) {
    setStepDetail('diag', d.diagnosis + '\n📦 Packages: ' + d.packages.join(', '));
  }
});

let _finished = false;  // terminal state — stop all reconnect/reload logic
let _handingOff = false; // true after handoff phase — server.py is starting up

es.addEventListener('done', e => {
  const d = JSON.parse(e.data);
  _finished = true;
  if (d.success) {
    badge.className = 'badge success';
    badge.textContent = '✅ Server starting — redirecting…';
    addStep('final', '🚀 Server ready!', 'done');
    // Wait a moment for the real server to bind the port, then redirect
    setTimeout(() => { window.location.href = '/'; }, 3000);
  } else {
    badge.className = 'badge failed';
    badge.textContent = '❌ Could not resolve — manual intervention needed';
    addStep('final', '❌ ' + (d.reason || 'Unresolvable error'), 'error');
    setStepDetail('final', d.hint
      ? d.hint
      : 'Please check the log output above and install dependencies manually.');
    // Always show API config form on error — user may need to configure credentials
    _showApiConfig();
  }
  es.close();
});

es.onerror = () => {
  // If we already reached a terminal state (done event), do NOT reconnect.
  if (_finished) return;
  // SSE disconnected — status server shut down to free port for server.py.
  // Poll until *some* server binds the port again: either the bootstrap
  // status server (next repair round) or the real Tofu server.
  es.close();
  badge.className = 'badge running';
  const _startTime = Date.now();
  const _elapsedStr = () => {
    const s = Math.floor((Date.now() - _startTime) / 1000);
    return s < 60 ? s + 's' : Math.floor(s/60) + 'm ' + (s%60) + 's';
  };
  if (_handingOff) {
    // Dependencies installed — server.py is starting (DB init, migrations, etc.)
    badge.innerHTML = '<span class="spinner"></span> Server starting up… (0s)';
    appendLog('Dependencies installed — waiting for server.py to start…', 'info');
  } else {
    badge.innerHTML = '<span class="spinner"></span> Reconnecting… (0s)';
  }
  let _pollCount = 0;
  const poll = setInterval(() => {
    _pollCount++;
    // Update elapsed time in badge
    if (_handingOff) {
      badge.innerHTML = '<span class="spinner"></span> Server starting up… (' + _elapsedStr() + ')';
    } else {
      badge.innerHTML = '<span class="spinner"></span> Reconnecting… (' + _elapsedStr() + ')';
    }
    fetch('/', { signal: AbortSignal.timeout(3000) }).then(async r => {
      if (!r.ok) return;
      // VS Code proxy fix: verify this is a real Tofu response, not a
      // stale proxy page or VS Code error page.  The real Tofu and the
      // bootstrap status page both return text/html — but we check for a
      // Tofu-specific marker to avoid reload loops with proxy pages.
      try {
        const text = await r.text();
        const isTofu = text.includes('Tofu') || text.includes('ChatUI')
                       || text.includes('bootstrap/events');
        if (isTofu) {
          clearInterval(poll);
          // If we were handing off and got the real Tofu, show success briefly
          if (_handingOff && !text.includes('bootstrap/events')) {
            badge.className = 'badge success';
            badge.textContent = '✅ Server ready — redirecting…';
          }
          window.location.reload();
        }
      } catch (_) {
        // Body read failed — keep polling
      }
    }).catch(() => {});
    // After 120s (60 polls), show a hint
    if (_pollCount === 60) {
      const hint = _handingOff
        ? ' (server startup is taking longer than expected — database initialization may be in progress)'
        : ' (if using VS Code port forwarding, try refreshing the page manually)';
      badge.innerHTML = '<span class="spinner"></span> ' +
        (_handingOff ? 'Server starting up' : 'Reconnecting') +
        '… (' + _elapsedStr() + ')' + hint;
    }
  }, 2000);
};
</script>
</body>
</html>
"""
class _BootstrapHandler(http.server.BaseHTTPRequestHandler):
    """Minimal HTTP handler for the bootstrap status page."""

    # Suppress default stderr logging for each request
    def log_message(self, format, *args):
        pass  # quiet — we have our own logging

    def do_GET(self):
        if self.path == '/' or self.path == '/index.html':
            self._serve_html()
        elif self.path == '/bootstrap/events':
            self._serve_sse()
        elif self.path == '/bootstrap/provider-templates':
            self._serve_provider_templates()
        else:
            # Any other path → serve the status page (user might hit /trading.html etc.)
            self._serve_html()

    def _serve_provider_templates(self):
        """Serve merged provider template list for the API config form."""
        try:
            templates = providers._load_provider_templates()
        except Exception as e:
            sys.stderr.write(f'[bootstrap] _load_provider_templates failed: {e}\n')
            templates = []
        self._json_response(templates)

    def do_POST(self):
        if self.path == '/bootstrap/save-config':
            self._handle_save_config()
        else:
            self.send_error(404)

    def _handle_save_config(self):
        """Save API config to .env file and signal restart."""
        try:
            length = int(self.headers.get('Content-Length', 0))
            body = json.loads(self.rfile.read(length).decode()) if length else {}
            api_key = body.get('api_key', '').strip()
            base_url = body.get('base_url', '').strip()
            model = body.get('model', '').strip()
            custom_model = bool(body.get('custom_model'))

            if not api_key:
                self._json_response({'ok': False, 'error': 'API key is required'})
                return

            # A release-bundled template is only a starting point. Prefer the
            # account's authenticated live catalogue before committing a
            # default, while keeping an explicitly typed private model pinned.
            templates = providers._load_provider_templates()
            live_models = providers._bootstrap_discover_models(
                base_url, api_key, templates=templates)
            if live_models and not custom_model:
                model = providers._bootstrap_choose_model(live_models, model) or model

            persist_models = live_models or providers._bootstrap_template_models(
                base_url, templates)
            persist_ids = {
                m.get('model_id') for m in persist_models if isinstance(m, dict)}
            if custom_model and model in persist_ids:
                for persisted in persist_models:
                    if persisted.get('model_id') == model:
                        persisted['catalog_pinned'] = True
                        persisted['catalog_source'] = 'manual'
                        break
            elif model and model not in persist_ids:
                caps = providers._bootstrap_infer_capabilities(model)
                persist_models.append({
                    'model_id': model,
                    'aliases': [],
                    'capabilities': caps,
                    'rpm': 30,
                    'cost': 0.01,
                    'thinking_default': 'thinking' in caps,
                    'catalog_pinned': True,
                    'catalog_source': 'manual',
                })

            # Write to .env file
            env_path = os.path.join(BASE_DIR, '.env')
            env_lines = []
            if os.path.exists(env_path):
                with open(env_path) as f:
                    env_lines = f.readlines()

            # Update or append each key
            _env_updates = {}
            if api_key:
                _env_updates['LLM_API_KEYS'] = api_key
            if base_url:
                _env_updates['LLM_BASE_URL'] = base_url
            if model:
                _env_updates['LLM_MODEL'] = model

            new_lines = []
            keys_written = set()
            for line in env_lines:
                stripped = line.strip()
                if stripped and not stripped.startswith('#') and '=' in stripped:
                    key = stripped.split('=', 1)[0].strip()
                    if key in _env_updates:
                        new_lines.append(f'{key}={_env_updates[key]}\n')
                        keys_written.add(key)
                        continue
                new_lines.append(line)

            # Append any keys not already in the file
            for key, val in _env_updates.items():
                if key not in keys_written:
                    new_lines.append(f'{key}={val}\n')

            with open(env_path, 'w') as f:
                f.writelines(new_lines)

            # Update current process env so retry picks up the new values
            os.environ['LLM_API_KEYS'] = api_key
            if base_url:
                os.environ['LLM_BASE_URL'] = base_url
            if model:
                os.environ['LLM_MODEL'] = model

            if persist_models and base_url:
                try:
                    providers._bootstrap_persist_provider(
                        base_url, api_key, persist_models,
                        templates=templates, default_model=model)
                except Exception as e:
                    # The .env values above remain a complete usable setup.
                    # Catalogue persistence is an optimisation, so do not turn
                    # a successful first-run configuration into an error page.
                    sys.stderr.write(
                        f'[bootstrap] Could not persist live model catalogue: {e}\n')

            print(f'[bootstrap] 💾 API config saved to {env_path}', file=sys.stderr)
            self._json_response({
                'ok': True,
                'model': model,
                'catalog_size': len(live_models),
            })

            # Signal the main thread to restart
            runtime._bus.emit('log', '💾 API config saved — restarting server…')
            # Set the restart flag so the main loop picks it up
            runtime.request_restart()

        except Exception as e:
            print(f'[bootstrap] ❌ Save config failed: {e}', file=sys.stderr)
            self._json_response({'ok': False, 'error': str(e)})

    def _json_response(self, data, status=200):
        body = json.dumps(data).encode('utf-8')
        self.send_response(status)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _serve_html(self):
        body = _STATUS_HTML.encode('utf-8')
        self.send_response(200)
        self.send_header('Content-Type', 'text/html; charset=utf-8')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _serve_sse(self):
        self.send_response(200)
        self.send_header('Content-Type', 'text/event-stream')
        self.send_header('Cache-Control', 'no-cache')
        self.send_header('Connection', 'keep-alive')
        self.send_header('X-Accel-Buffering', 'no')
        self.end_headers()

        q = runtime._bus.subscribe()
        try:
            while True:
                try:
                    evt = q.get(timeout=30)
                except queue.Empty:
                    # Keepalive comment
                    self.wfile.write(b': keepalive\n\n')
                    self.wfile.flush()
                    continue
                sse = f"event: {evt['event']}\ndata: {evt['data']}\n\n"
                self.wfile.write(sse.encode('utf-8'))
                self.wfile.flush()
                # If the 'done' event was sent, allow a moment then stop
                if evt['event'] == 'done':
                    time.sleep(1)
                    break
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass
        finally:
            runtime._bus.unsubscribe(q)
class _QuietServer(http.server.HTTPServer):
    """HTTPServer that doesn't print to stderr on broken pipes."""
    def handle_error(self, request, client_address):
        pass  # suppress tracebacks from disconnected browsers
def _find_free_port(host: str, start_port: int, max_tries: int = 20) -> int | None:
    """Scan upward from *start_port* to find a free TCP port.

    Returns the first available port, or None if all tried ports are busy.
    """
    for offset in range(max_tries):
        candidate = start_port + offset
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                s.bind((host, candidate))
                return candidate
        except OSError:
            continue
    return None
def _start_status_server(host: str, port: int) -> http.server.HTTPServer | None:
    """Start the mini status server in a daemon thread.

    If *port* is already in use, automatically scans upward for a free
    port (like the PostgreSQL auto-bootstrap).  When a different port is
    chosen, ``os.environ['PORT']`` is updated so that subsequent
    ``server.py`` launches inherit the new port.

    Returns the server, or None if no free port could be found.
    """
    chosen_port = port
    try:
        server = _QuietServer((host, port), _BootstrapHandler)
    except OSError:
        # Configured port is busy — scan for a free one
        free = _find_free_port(host, port + 1)
        if free is None:
            print(f'[bootstrap] ⚠ Cannot bind {host}:{port} and no free port '
                  f'found in range {port+1}–{port+20}', file=sys.stderr)
            return None
        try:
            server = _QuietServer((host, free), _BootstrapHandler)
        except OSError as e2:
            print(f'[bootstrap] ⚠ Cannot bind {host}:{free}: {e2}',
                  file=sys.stderr)
            return None
        chosen_port = free
        # Propagate the new port so server.py also uses it
        os.environ['PORT'] = str(chosen_port)
        print(f'[bootstrap] ⚠ Port {port} in use — auto-switched to {chosen_port}',
              file=sys.stderr)
    t = threading.Thread(target=server.serve_forever, daemon=True, name='BootstrapStatusServer')
    t.start()
    print(f'[bootstrap] 🔧 Status page: http://localhost:{chosen_port}/', file=sys.stderr)
    return server
def _stop_status_server(server: http.server.HTTPServer | None) -> None:
    """Shut down the mini status server and release the port."""
    if server is None:
        return
    server.shutdown()
    server.server_close()
    time.sleep(0.5)  # let OS release the port
