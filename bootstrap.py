#!/usr/bin/env python3
"""bootstrap.py — Smart server launcher with LLM-guided dependency repair.

Usage:  python bootstrap.py          (drop-in replacement for python server.py)

Behaviour:
  1. Try to start server.py normally.
  2. If it crashes (usually a missing package), spin up a tiny status page
     on the same port so the user can watch progress in the browser.
  3. Send the traceback to the project's LLM API for analysis.
  4. Install whatever packages the LLM recommends (pip install).
  5. Retry — loop until success or the error is deemed unresolvable.

If server.py starts cleanly, this script is 100 % transparent — the user
sees exactly the same output as running ``python server.py`` directly.

IMPORTANT: This file uses ONLY the Python standard library.  It must work
even when *every* pip package is missing (that's the whole point).
"""

from __future__ import annotations

import http.server
import json
import os
import queue
import re
import signal
import socket
import subprocess
import sys
import textwrap
import threading
import time
import traceback
import urllib.error
import urllib.parse
import urllib.request

# ══════════════════════════════════════════════════════════
#  Configuration (mirrors server.py / lib/__init__.py)
# ══════════════════════════════════════════════════════════

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
MAX_REPAIR_ROUNDS = 10       # give up after this many install→retry cycles
PIP_TIMEOUT = 300            # per-package install timeout
# Packages that should never be auto-installed (security / system-level)
_INSTALL_BLOCKLIST = frozenset({
    'python', 'python3', 'gcc', 'g++', 'make', 'cmake', 'apt', 'yum',
    'brew', 'sudo', 'pip', 'setuptools', 'wheel',
})


def _load_dotenv() -> None:
    """Load .env file (same logic as server.py)."""
    env_path = os.path.join(BASE_DIR, '.env')
    if not os.path.exists(env_path):
        return
    with open(env_path) as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith('#') or '=' not in line:
                continue
            key, _, value = line.partition('=')
            key, value = key.strip(), value.strip()
            if key not in os.environ:
                os.environ[key] = value

_load_dotenv()


def _get_config():
    """Read LLM config from env (same defaults as lib/__init__.py)."""
    keys_env = os.environ.get('LLM_API_KEYS', '')
    if keys_env:
        api_keys = [k.strip() for k in keys_env.split(',') if k.strip()]
    else:
        single = os.environ.get('LLM_API_KEY', '')
        api_keys = [single] if single else []
    return {
        'api_keys': api_keys,
        'base_url': os.environ.get(
            'LLM_BASE_URL',
            'https://api.openai.com/v1'),
        'model': os.environ.get('LLM_MODEL', 'gpt-4.1-mini'),
        'host': os.environ.get('BIND_HOST', '0.0.0.0'),
        'port': int(os.environ.get('PORT', 15000)),
    }


# ══════════════════════════════════════════════════════════
#  Thread-safe SSE event bus
# ══════════════════════════════════════════════════════════

class EventBus:
    """Pub/sub for SSE events.  Multiple browser tabs can subscribe."""

    def __init__(self):
        self._subscribers: list[queue.Queue] = []
        self._lock = threading.Lock()
        self._history: list[dict] = []       # replay for late joiners

    def subscribe(self) -> queue.Queue:
        q: queue.Queue = queue.Queue(maxsize=200)
        with self._lock:
            # send history
            for evt in self._history:
                q.put(evt)
            self._subscribers.append(q)
        return q

    def unsubscribe(self, q: queue.Queue) -> None:
        with self._lock:
            try:
                self._subscribers.remove(q)
            except ValueError:
                pass

    def emit(self, event: str, data: str | dict) -> None:
        payload = data if isinstance(data, str) else json.dumps(data)
        evt = {'event': event, 'data': payload}
        with self._lock:
            self._history.append(evt)
            dead = []
            for q in self._subscribers:
                try:
                    q.put_nowait(evt)
                except queue.Full:
                    dead.append(q)
            for q in dead:
                try:
                    self._subscribers.remove(q)
                except ValueError:
                    pass


_bus = EventBus()
_restart_requested = False  # Set by POST /bootstrap/save-config to trigger server retry


# ══════════════════════════════════════════════════════════
#  LLM API call (pure stdlib — urllib only)
# ══════════════════════════════════════════════════════════

def _call_llm(error_text: str, cfg: dict) -> dict:
    """Ask the LLM to diagnose the traceback and suggest pip packages.

    Returns dict: {"packages": ["pkg1", ...], "diagnosis": "...", "unresolvable": bool}
    """
    url = cfg['base_url'].rstrip('/') + '/chat/completions'
    prompt = textwrap.dedent(f"""\
        You are a Python dependency troubleshooter.

        The user ran ``python server.py`` and got the error below.
        Your job:
        1. Diagnose the root cause.
        2. If the fix is to ``pip install`` one or more packages, list them.
        3. If the error is NOT fixable via pip (e.g. wrong Python version,
           missing C libraries, code bugs), set "unresolvable" to true
           and explain why in "diagnosis".

        RULES:
        - Return ONLY valid JSON — no markdown fences, no commentary.
        - Package names must be pip-installable names
          (e.g. "flask-compress" not "flask_compress").
        - If a ModuleNotFoundError names a module like "foo.bar",
          the pip package is usually just "foo" — but use your knowledge
          to map correctly (e.g. module "cv2" → pip "opencv-python").
        - When you see a missing package, also proactively include closely
          related packages that the same project likely needs.  For example,
          if "flask" is missing, also suggest "flask-compress" and "requests"
          since web servers almost always need them.
        - Never suggest system packages (apt/yum), only pip packages.
        - EXCEPTION: if the error is about missing PostgreSQL binaries
          (initdb, pg_ctl, pg_isready), set "unresolvable" to false and
          put "conda:postgresql>=18" in the packages list.  The installer
          knows how to handle conda: prefixed packages specially.

        Respond with this JSON schema:
        {{
          "packages": ["pkg1", "pkg2"],
          "diagnosis": "Human-readable explanation",
          "unresolvable": false
        }}

        --- ERROR OUTPUT ---
        {error_text[-6000:]}
        --- END ---
    """)

    body = json.dumps({
        'model': cfg['model'],
        'messages': [{'role': 'user', 'content': prompt}],
        'max_tokens': 1024,
        'temperature': 0.2,
        'stream': False,
    }).encode()

    # Try each API key until one works
    last_err = None
    for key in cfg['api_keys']:
        headers = {
            'Content-Type': 'application/json',
            'Authorization': f'Bearer {key}',
        }
        req = urllib.request.Request(url, data=body, headers=headers)
        try:
            # Handle proxy bypass for internal domains
            host = urllib.parse.urlparse(url).hostname or ''
            _bypass = os.environ.get('PROXY_BYPASS_DOMAINS', '')
            _bypass_suffixes = tuple(d.strip() for d in _bypass.split(',') if d.strip())
            if _bypass_suffixes and host.endswith(_bypass_suffixes):
                proxy_handler = urllib.request.ProxyHandler({})
                opener = urllib.request.build_opener(proxy_handler)
            else:
                opener = urllib.request.build_opener()
            with opener.open(req, timeout=60) as resp:
                raw = json.loads(resp.read().decode())
            content = raw['choices'][0]['message']['content']
            # Strip markdown fences if present
            content = re.sub(r'^```(?:json)?\s*', '', content.strip())
            content = re.sub(r'\s*```$', '', content.strip())
            return json.loads(content)
        except Exception as e:
            last_err = e
            continue

    return {
        'packages': [],
        'diagnosis': f'Could not reach LLM API to diagnose the error: {last_err}',
        'unresolvable': True,
    }


# ══════════════════════════════════════════════════════════
#  requirements.txt fast path (no LLM needed)
# ══════════════════════════════════════════════════════════

def _try_requirements_txt() -> bool:
    """Try to install all packages from requirements.txt.

    This is the fast path: if a requirements.txt exists, we can install
    everything from it without needing the LLM at all.  This is critical
    for freshly-exported projects where the LLM API keys haven't been
    configured yet.

    Returns True if requirements.txt was found and pip succeeded.
    """
    req_path = os.path.join(BASE_DIR, 'requirements.txt')
    if not os.path.isfile(req_path):
        return False

    _bus.emit('phase', json.dumps({
        'id': 'reqtxt',
        'label': '📋 Found requirements.txt — installing all dependencies…',
        'status': 'active',
    }))
    _bus.emit('log', f'Found {req_path}')

    cmd = [sys.executable, '-m', 'pip', 'install', '--no-input', '-r', req_path]
    _bus.emit('log', f'$ {" ".join(cmd)}')

    try:
        proc = subprocess.Popen(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, bufsize=1, cwd=BASE_DIR)
    except Exception as e:
        _bus.emit('log', f'Failed to run pip: {e}')
        _bus.emit('phase', json.dumps({
            'id': 'reqtxt',
            'label': '📋 requirements.txt — pip failed to start',
            'status': 'error',
        }))
        return False

    for line in proc.stdout:
        line = line.rstrip('\n')
        _bus.emit('pip_output', line)

    proc.wait(timeout=PIP_TIMEOUT)

    if proc.returncode == 0:
        _bus.emit('log', '✅ pip install -r requirements.txt succeeded')
        _bus.emit('phase', json.dumps({
            'id': 'reqtxt',
            'label': '📋 requirements.txt — all dependencies installed',
            'status': 'done',
        }))
        return True
    else:
        _bus.emit('log', f'❌ pip install -r requirements.txt failed (exit code {proc.returncode})')
        _bus.emit('phase', json.dumps({
            'id': 'reqtxt',
            'label': '📋 requirements.txt — pip install failed',
            'status': 'error',
            'detail': f'Exit code {proc.returncode}. Some packages may need system-level deps.',
        }))
        return False


# ══════════════════════════════════════════════════════════
#  conda-based PostgreSQL auto-install
# ══════════════════════════════════════════════════════════

def _need_pg_install() -> bool:
    """Check if PostgreSQL binaries are missing from PATH."""
    import shutil
    return shutil.which('initdb') is None or shutil.which('pg_ctl') is None


def _try_conda_install_postgresql() -> bool:
    """Try to install PostgreSQL via conda if PG binaries are missing.

    Returns True if installation succeeded (or PG was already available).
    Returns False if conda is not available or installation failed.
    """
    if not _need_pg_install():
        return True  # already available

    # Check if conda is available
    import shutil
    conda_bin = shutil.which('conda')
    if not conda_bin:
        # Also try mamba (faster conda alternative)
        conda_bin = shutil.which('mamba')
    if not conda_bin:
        _bus.emit('log', '⚠ PostgreSQL binaries not found and conda/mamba not available — '
                         'please install PostgreSQL manually: conda install -c conda-forge postgresql>=18')
        return False

    _bus.emit('phase', json.dumps({
        'id': 'conda-pg',
        'label': '🐘 PostgreSQL not found — installing via conda…',
        'status': 'active',
    }))

    cmd = [conda_bin, 'install', '-c', 'conda-forge', '-y', 'postgresql>=18']
    _bus.emit('log', f'$ {" ".join(cmd)}')

    try:
        proc = subprocess.Popen(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, bufsize=1, cwd=BASE_DIR)
    except Exception as e:
        _bus.emit('log', f'Failed to run conda: {e}')
        _bus.emit('phase', json.dumps({
            'id': 'conda-pg',
            'label': '🐘 conda install failed to start',
            'status': 'error',
        }))
        return False

    for line in proc.stdout:
        line = line.rstrip('\n')
        _bus.emit('pip_output', line)  # reuse pip_output event for live log

    proc.wait(timeout=600)  # conda can be slow

    if proc.returncode == 0 and not _need_pg_install():
        _bus.emit('log', '✅ PostgreSQL installed via conda')
        _bus.emit('phase', json.dumps({
            'id': 'conda-pg',
            'label': '🐘 PostgreSQL installed successfully',
            'status': 'done',
        }))
        return True
    else:
        _bus.emit('log', f'❌ conda install postgresql failed (exit code {proc.returncode})')
        _bus.emit('phase', json.dumps({
            'id': 'conda-pg',
            'label': '🐘 conda install postgresql failed',
            'status': 'error',
            'detail': f'Exit code {proc.returncode}. Install manually: '
                       'conda install -c conda-forge postgresql>=18',
        }))
        return False


# ══════════════════════════════════════════════════════════
#  pip installer with live output
# ══════════════════════════════════════════════════════════

def _pip_install(packages: list[str]) -> tuple[bool, str]:
    """Run pip install for the given packages, emitting SSE progress.

    Packages prefixed with ``conda:`` (e.g. ``conda:postgresql>=18``) are
    installed via conda/mamba instead of pip.

    Returns (success: bool, output: str).
    """
    # Separate conda packages from pip packages
    conda_pkgs = [p[6:] for p in packages if p.startswith('conda:')]
    pip_pkgs = [p for p in packages if not p.startswith('conda:')]

    # Install conda packages first (e.g. postgresql)
    if conda_pkgs:
        _bus.emit('log', f'🐘 Detected conda packages: {conda_pkgs}')
        _try_conda_install_postgresql()  # currently the only conda package we support

    # Filter out blocked packages
    safe_pkgs = [p for p in pip_pkgs if p.lower() not in _INSTALL_BLOCKLIST]
    if not safe_pkgs and not conda_pkgs:
        return False, 'All suggested packages are in the blocklist.'
    if not safe_pkgs:
        return True, 'Only conda packages were requested (handled separately).'

    cmd = [sys.executable, '-m', 'pip', 'install', '--no-input'] + safe_pkgs
    _bus.emit('log', f'$ {" ".join(cmd)}')

    try:
        proc = subprocess.Popen(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, bufsize=1, cwd=BASE_DIR)
    except Exception as e:
        msg = f'Failed to run pip: {e}'
        _bus.emit('log', msg)
        return False, msg

    output_lines = []
    for line in proc.stdout:
        line = line.rstrip('\n')
        output_lines.append(line)
        _bus.emit('pip_output', line)

    proc.wait(timeout=PIP_TIMEOUT)
    full_output = '\n'.join(output_lines)

    if proc.returncode == 0:
        _bus.emit('log', '✅ pip install succeeded')
        return True, full_output
    else:
        _bus.emit('log', f'❌ pip install failed (exit code {proc.returncode})')
        return False, full_output


# ══════════════════════════════════════════════════════════
#  Mini HTTP status server (stdlib only)
# ══════════════════════════════════════════════════════════

_STATUS_HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>ChatUI — Starting…</title>
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
  <h1>🔧 ChatUI — Dependency Repair</h1>
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
      ChatUI needs an LLM API key to function. Enter your API credentials below.
      This will save to your <code>.env</code> file and restart the server.
    </p>

    <div class="provider-templates">
      <span class="provider-tpl active" onclick="_selectTemplate('openai')">OpenAI</span>
      <span class="provider-tpl" onclick="_selectTemplate('anthropic')">Anthropic</span>
      <span class="provider-tpl" onclick="_selectTemplate('deepseek')">DeepSeek</span>
      <span class="provider-tpl" onclick="_selectTemplate('openrouter')">OpenRouter</span>
      <span class="provider-tpl" onclick="_selectTemplate('custom')">Custom</span>
    </div>

    <label for="cfgApiKey">API Key <span style="color:var(--red)">*</span></label>
    <input type="password" id="cfgApiKey" placeholder="sk-…" autocomplete="off">

    <label for="cfgBaseUrl">Base URL</label>
    <input type="text" id="cfgBaseUrl" value="https://api.openai.com/v1" placeholder="https://api.openai.com/v1">

    <label for="cfgModel">Model</label>
    <input type="text" id="cfgModel" value="gpt-4.1-mini" placeholder="gpt-4.1-mini">

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

// ── Provider template presets ──
const _TEMPLATES = {
  openai:     { url: 'https://api.openai.com/v1',   model: 'gpt-5.4' },
  anthropic:  { url: 'https://api.anthropic.com/v1', model: 'claude-sonnet-4-6' },
  deepseek:   { url: 'https://api.deepseek.com/v1',  model: 'deepseek-chat' },
  openrouter: { url: 'https://openrouter.ai/api/v1', model: 'anthropic/claude-sonnet-4.6' },
  custom:     { url: '',                              model: '' },
};
function _selectTemplate(name) {
  const t = _TEMPLATES[name] || _TEMPLATES.custom;
  document.getElementById('cfgBaseUrl').value = t.url;
  document.getElementById('cfgModel').value = t.model;
  document.querySelectorAll('.provider-tpl').forEach(el => {
    el.classList.toggle('active', el.textContent.toLowerCase().replace(/\s/g,'') === name);
  });
}
function _showApiConfig() {
  document.getElementById('apiConfigOverlay').classList.add('visible');
  // Auto-focus the API key field
  setTimeout(() => document.getElementById('cfgApiKey').focus(), 300);
}
function _saveApiConfig() {
  const key = document.getElementById('cfgApiKey').value.trim();
  const url = document.getElementById('cfgBaseUrl').value.trim();
  const model = document.getElementById('cfgModel').value.trim();
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
    body: JSON.stringify({ api_key: key, base_url: url, model: model })
  }).then(r => r.json()).then(d => {
    if (d.ok) {
      status.textContent = '✅ Saved! Restarting server…';
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
  // status server (next repair round) or the real ChatUI server.
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
      // VS Code proxy fix: verify this is a real ChatUI response, not a
      // stale proxy page or VS Code error page.  The real ChatUI and the
      // bootstrap status page both return text/html — but we check for a
      // ChatUI-specific marker to avoid reload loops with proxy pages.
      try {
        const text = await r.text();
        const isChatUI = text.includes('ChatUI') || text.includes('Tofu')
                       || text.includes('bootstrap/events');
        if (isChatUI) {
          clearInterval(poll);
          // If we were handing off and got the real ChatUI, show success briefly
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
        else:
            # Any other path → serve the status page (user might hit /trading.html etc.)
            self._serve_html()

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

            if not api_key:
                self._json_response({'ok': False, 'error': 'API key is required'})
                return

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

            print(f'[bootstrap] 💾 API config saved to {env_path}', file=sys.stderr)
            self._json_response({'ok': True})

            # Signal the main thread to restart
            _bus.emit('log', '💾 API config saved — restarting server…')
            # Set the restart flag so the main loop picks it up
            global _restart_requested
            _restart_requested = True

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

        q = _bus.subscribe()
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
            _bus.unsubscribe(q)


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


# ══════════════════════════════════════════════════════════
#  Server process launcher & error capture
# ══════════════════════════════════════════════════════════

def _try_start_server(first_attempt: bool = False) -> tuple[bool, str, int]:
    """Attempt to start server.py.

    A healthy server.py runs ``app.run()`` which **blocks forever**.
    If the subprocess *returns at all*, it crashed.  We simply call
    ``proc.wait()`` with no timeout:

    - Process crashes (import error, etc.) → returns instantly with
      ``(False, captured_stderr, exit_code)``.
    - Process runs successfully → ``proc.wait()`` blocks forever
      (transparent pass-through).  On Ctrl+C or clean shutdown
      (exit code 0), calls ``sys.exit(0)`` — never returns to caller.

    This function only returns to the caller when server.py **crashed**.
    """
    env = os.environ.copy()
    env['_CHATUI_VIA_BOOTSTRAP'] = '1'   # prevent server.py → bootstrap.py re-delegation loop
    proc = subprocess.Popen(
        [sys.executable, os.path.join(BASE_DIR, 'server.py')],
        stdout=sys.stdout,     # always forward stdout transparently
        stderr=subprocess.PIPE,
        text=True,
        cwd=BASE_DIR,
        env=env,
    )

    stderr_lines = []
    stderr_done = threading.Event()

    def _read_stderr():
        """Read stderr in background — forward to our stderr AND capture it."""
        try:
            for line in proc.stderr:
                sys.stderr.write(line)
                sys.stderr.flush()
                stderr_lines.append(line)
        except (ValueError, OSError):
            pass
        finally:
            stderr_done.set()

    reader = threading.Thread(target=_read_stderr, daemon=True)
    reader.start()

    # Forward signals so Ctrl+C in the terminal reaches server.py
    def _forward_signal(signum, frame):
        try:
            proc.send_signal(signum)
        except OSError:
            pass

    prev_sigint = signal.signal(signal.SIGINT, _forward_signal)
    prev_sigterm = None
    if hasattr(signal, 'SIGTERM'):
        prev_sigterm = signal.signal(signal.SIGTERM, _forward_signal)

    try:
        rc = proc.wait()       # blocks until server.py exits (crash or Ctrl+C)
    except KeyboardInterrupt:
        # User hit Ctrl+C — clean shutdown
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
        sys.exit(0)
    finally:
        # Restore original signal handlers so bootstrap can still be interrupted
        signal.signal(signal.SIGINT, prev_sigint)
        if prev_sigterm is not None and hasattr(signal, 'SIGTERM'):
            signal.signal(signal.SIGTERM, prev_sigterm)

    stderr_done.wait(timeout=5)
    stderr_text = ''.join(stderr_lines)

    # Exit code 0 means graceful shutdown (user hit Ctrl+C, SIGTERM, etc.)
    # — that's not a crash, it's intentional.
    if rc == 0:
        sys.exit(0)

    # Non-zero exit → crash.
    return False, stderr_text, rc


def _is_import_or_package_error(stderr_text: str) -> bool:
    """Heuristic: does the traceback look like a missing-package error?"""
    indicators = [
        'ModuleNotFoundError',
        'ImportError',
        'No module named',
        'cannot import name',
        'pkg_resources.DistributionNotFound',
        'ModuleNotFoundError',
        'No matching distribution found',
    ]
    return any(ind in stderr_text for ind in indicators)


def _is_pg_missing_error(stderr_text: str) -> bool:
    """Heuristic: does the error look like PostgreSQL binaries are missing?"""
    indicators = [
        'initdb not found',
        'pg_ctl not found',
        'FileNotFoundError',  # combined with pg binary names
        'install PostgreSQL',
        'conda install',
        'postgresql',
    ]
    text_lower = stderr_text.lower()
    # Must mention PG-related terms
    pg_terms = ['initdb', 'pg_ctl', 'postgresql', 'postgres']
    has_pg = any(t in text_lower for t in pg_terms)
    has_error = any(ind.lower() in text_lower for ind in indicators[:3])
    return has_pg and has_error


# ══════════════════════════════════════════════════════════
#  Main bootstrap loop
# ══════════════════════════════════════════════════════════

def main():
    global _bus  # may be reset when premature 'done' events need clearing
    cfg = _get_config()
    host = cfg['host']
    port = cfg['port']
    has_llm = bool(cfg['api_keys'])

    # ── Auto-detect free port if configured port is busy ──
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as _s:
            _s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            _s.bind((host, port))
    except OSError:
        free = _find_free_port(host, port + 1)
        if free is not None:
            print(f'[bootstrap] ⚠ Port {port} in use — auto-switched to {free}',
                  file=sys.stderr)
            port = free
            os.environ['PORT'] = str(port)
            cfg['port'] = port
        else:
            print(f'[bootstrap] ⚠ Port {port} in use and no free port found '
                  f'in range {port+1}–{port+20}', file=sys.stderr)

    print(f'[bootstrap] 🚀 Starting ChatUI (host={host}, port={port})…',
          file=sys.stderr)

    # ── First attempt (fast path — no status page) ──
    # A healthy server.py blocks forever (app.run).  If _try_start_server
    # returns at all, the process crashed.  On clean shutdown (rc=0, e.g.
    # Ctrl+C) it calls sys.exit(0) internally — so reaching here means crash.
    _, stderr_text, rc = _try_start_server(first_attempt=True)

    # ── Enter repair mode ──
    print(f'[bootstrap] ⚠ server.py crashed (exit code {rc}). '
          f'Entering dependency repair mode…', file=sys.stderr)

    status_server = _start_status_server(host, port)

    _bus.emit('phase', json.dumps({
        'id': 'crash-0', 'label': '💥 Server crashed on startup',
        'status': 'error',
        'detail': f'Exit code {rc}',
    }))
    _bus.emit('error_text', stderr_text[-3000:])

    # ── Fast path: requirements.txt (no LLM needed) ──
    # For import / package errors, try installing from requirements.txt
    # first.  This is essential for freshly-exported projects where the
    # LLM API hasn't been configured yet.
    if _is_import_or_package_error(stderr_text) and _try_requirements_txt():
        # ── Also install PostgreSQL via conda if needed ──
        # After pip deps are installed, server.py may crash because PG
        # binaries (initdb, pg_ctl) are missing — they're not pip packages.
        _try_conda_install_postgresql()

        _bus.emit('phase', json.dumps({
            'id': 'reqtxt-retry',
            'label': '🔄 Retrying server.py after requirements.txt install…',
            'status': 'active',
        }))
        _bus.emit('log', 'Restarting server.py…')

        # ── Notify browser, then free the port for server.py ──
        # Do NOT emit a 'done' event here — server.py hasn't started yet.
        # Instead, emit a 'handoff' phase so the user knows what's happening.
        # When the status server shuts down, the browser's SSE connection drops,
        # es.onerror fires (with _finished=false), and the reconnect polling
        # begins.  The poll will find server.py once it's ready to serve HTTP.
        _bus.emit('phase', json.dumps({
            'id': 'handoff',
            'label': '🔄 Handing off to server.py — this may take a moment…',
            'status': 'active',
            'detail': 'The server is starting up (database init, migrations, etc.).',
        }))
        time.sleep(0.5)  # give browsers time to receive the phase event

        _stop_status_server(status_server)
        status_server = None

        _, stderr_text, rc = _try_start_server()
        # If _try_start_server returns, the server crashed again.

        if _is_import_or_package_error(stderr_text):
            # Still import errors after requirements.txt — fall through to LLM
            print(f'[bootstrap] ⚠ Still failing after requirements.txt install.',
                  file=sys.stderr)
            # Reset event bus so new browsers get a clean history
            _bus = EventBus()
            status_server = _start_status_server(host, port)
            _bus.emit('phase', json.dumps({
                'id': 'reqtxt-retry',
                'label': '🔄 Still failing after requirements.txt — trying LLM diagnosis…',
                'status': 'error',
            }))
            _bus.emit('error_text', stderr_text[-3000:])
        elif _is_pg_missing_error(stderr_text):
            # PostgreSQL binaries missing — try conda install
            # Reset event bus so new browsers get a clean history
            _bus = EventBus()
            status_server = _start_status_server(host, port)
            _bus.emit('phase', json.dumps({
                'id': 'reqtxt-retry',
                'label': '🔄 Still failing — PostgreSQL binaries missing',
                'status': 'error',
                'detail': f'Exit code {rc}',
            }))
            _bus.emit('error_text', stderr_text[-3000:])

            if _try_conda_install_postgresql():
                # PG installed — retry server
                _bus.emit('phase', json.dumps({
                    'id': 'pg-retry',
                    'label': '🔄 Retrying server.py after PostgreSQL install…',
                    'status': 'active',
                }))
                _bus.emit('phase', json.dumps({
                    'id': 'handoff',
                    'label': '🔄 Handing off to server.py — this may take a moment…',
                    'status': 'active',
                    'detail': 'The server is starting up (database init, migrations, etc.).',
                }))
                time.sleep(0.5)
                _stop_status_server(status_server)
                status_server = None

                _, stderr_text, rc = _try_start_server()
                # If _try_start_server returns, the server crashed again.
                # Reset and fall through to LLM repair
                _bus = EventBus()
                status_server = _start_status_server(host, port)
                _bus.emit('phase', json.dumps({
                    'id': 'pg-retry',
                    'label': '🔄 Still failing after PostgreSQL install',
                    'status': 'error',
                    'detail': f'Exit code {rc}',
                }))
                _bus.emit('error_text', stderr_text[-3000:])
            # else: conda install failed — fall through to LLM or manual
        else:
            # Non-import error or still crashing for a different reason
            # Reset event bus so new browsers get a clean history
            _bus = EventBus()
            status_server = _start_status_server(host, port)
            _bus.emit('phase', json.dumps({
                'id': 'reqtxt-retry',
                'label': '🔄 Still failing (non-dependency error)',
                'status': 'error',
                'detail': f'Exit code {rc}',
            }))
            _bus.emit('error_text', stderr_text[-3000:])

            if not has_llm:
                _bus.emit('done', json.dumps({
                    'success': False,
                    'reason': 'Server still crashing after installing requirements.txt. '
                              'The error does not look like a missing-package issue.',
                    'hint': 'Check the error log above. You may also need to configure '
                            'LLM API credentials in .env (LLM_API_KEY, LLM_BASE_URL) '
                            'for smarter auto-diagnosis.',
                }))
                print(f'[bootstrap] ❌ Non-dependency error and no LLM API configured.',
                      file=sys.stderr)
                _keep_alive_until_interrupt(status_server)
                return

    # ── Check if LLM is available for diagnosis ──
    if not has_llm:
        hint_lines = [
            'No LLM API key configured — cannot auto-diagnose.',
            '',
            'To fix manually:',
            '  1. pip install -r requirements.txt',
            '  2. Configure LLM credentials in .env:',
            '     LLM_API_KEY=sk-your-key-here',
            '     LLM_BASE_URL=https://api.openai.com/v1',
            '  3. Re-run: python server.py',
        ]
        hint = '\n'.join(hint_lines)
        _bus.emit('log', hint)
        _bus.emit('done', json.dumps({
            'success': False,
            'reason': 'No LLM API key configured. Cannot auto-diagnose the error.',
            'hint': 'Set LLM_API_KEY and LLM_BASE_URL in .env, then run '
                    '"pip install -r requirements.txt" and "python server.py".',
        }))
        print(f'[bootstrap] ❌ No LLM API key configured. '
              f'Set LLM_API_KEY in .env and retry.', file=sys.stderr)
        _keep_alive_until_interrupt(status_server)
        return

    # ── LLM-guided repair loop ──
    _bus.emit('round', json.dumps({'current': 1, 'max': MAX_REPAIR_ROUNDS}))

    installed_so_far: list[str] = []
    prev_error = ''

    for round_num in range(1, MAX_REPAIR_ROUNDS + 1):
        _bus.emit('round', json.dumps({'current': round_num, 'max': MAX_REPAIR_ROUNDS}))

        # ── Phase 1: Analyse with LLM ──
        _bus.emit('phase', json.dumps({
            'id': f'llm-{round_num}', 'label': f'🤖 Round {round_num}: Asking LLM to diagnose…',
            'status': 'active',
        }))
        _bus.emit('log', f'── Round {round_num}/{MAX_REPAIR_ROUNDS} ──')

        # Add context about previous installs so LLM doesn't suggest the same thing
        context = stderr_text
        if installed_so_far:
            context += f'\n\n[CONTEXT] Already installed in previous rounds: {", ".join(installed_so_far)}'

        result = _call_llm(context, cfg)
        diagnosis = result.get('diagnosis', 'No diagnosis available.')
        packages = result.get('packages', [])
        unresolvable = result.get('unresolvable', False)

        _bus.emit('diagnosis', json.dumps({
            'diagnosis': diagnosis,
            'packages': packages,
            'unresolvable': unresolvable,
        }))
        _bus.emit('phase', json.dumps({
            'id': f'llm-{round_num}', 'label': f'🤖 Round {round_num}: Diagnosis complete',
            'status': 'done',
            'detail': diagnosis[:200],
        }))

        if unresolvable:
            _bus.emit('log', f'LLM says this error is not fixable via pip: {diagnosis}')
            _bus.emit('done', json.dumps({
                'success': False,
                'reason': diagnosis,
            }))
            print(f'[bootstrap] ❌ Unresolvable error: {diagnosis}', file=sys.stderr)
            # Keep status server alive so user can read the page
            _keep_alive_until_interrupt(status_server)
            return

        if not packages:
            _bus.emit('log', 'LLM did not suggest any packages. Retrying with raw error…')
            # One more attempt: maybe the LLM response was malformed
            if round_num >= 3:
                _bus.emit('done', json.dumps({
                    'success': False,
                    'reason': 'LLM could not determine which packages to install.',
                }))
                _keep_alive_until_interrupt(status_server)
                return
            continue

        # ── Phase 2: Install packages ──
        new_pkgs = [p for p in packages if p not in installed_so_far]
        if not new_pkgs:
            _bus.emit('log', f'All suggested packages already installed: {packages}')
            # Same packages suggested again → likely not a pip issue
            _bus.emit('done', json.dumps({
                'success': False,
                'reason': f'Already installed {packages} but error persists. Manual intervention needed.',
            }))
            _keep_alive_until_interrupt(status_server)
            return

        _bus.emit('phase', json.dumps({
            'id': f'pip-{round_num}',
            'label': f'📦 Round {round_num}: Installing {", ".join(new_pkgs)}',
            'status': 'active',
        }))

        pip_ok, pip_output = _pip_install(new_pkgs)

        if pip_ok:
            installed_so_far.extend(new_pkgs)
            _bus.emit('phase', json.dumps({
                'id': f'pip-{round_num}',
                'label': f'📦 Round {round_num}: Installed {", ".join(new_pkgs)}',
                'status': 'done',
            }))
        else:
            _bus.emit('phase', json.dumps({
                'id': f'pip-{round_num}',
                'label': f'📦 Round {round_num}: pip install failed',
                'status': 'error',
                'detail': 'See log output for details.',
            }))
            # pip failure is potentially unresolvable
            _bus.emit('done', json.dumps({
                'success': False,
                'reason': f'pip install failed for: {", ".join(new_pkgs)}',
            }))
            _keep_alive_until_interrupt(status_server)
            return

        # ── Phase 3: Retry server.py ──
        _bus.emit('phase', json.dumps({
            'id': f'retry-{round_num}',
            'label': f'🔄 Round {round_num}: Retrying server.py…',
            'status': 'active',
        }))
        _bus.emit('log', 'Restarting server.py…')

        # Notify browsers: "we're about to restart — reconnect shortly"
        # Do NOT emit a 'done' event here — server.py hasn't started yet.
        # Let the SSE drop naturally so the browser's reconnect polling kicks in.
        _bus.emit('phase', json.dumps({
            'id': f'handoff-{round_num}',
            'label': f'🔄 Round {round_num}: Handing off to server.py — this may take a moment…',
            'status': 'active',
            'detail': 'The server is starting up (database init, migrations, etc.).',
        }))
        time.sleep(0.5)  # give browsers time to receive the phase event

        # Stop status server to free the port before retrying
        _stop_status_server(status_server)
        status_server = None

        _, stderr_text, rc = _try_start_server()
        # If _try_start_server returns, the server crashed again.
        # (On success it blocks forever; on clean exit it calls sys.exit.)

        # Still crashing — re-start status page for next round
        # Reset event bus so new browsers get a clean history
        _bus = EventBus()
        _bus.emit('phase', json.dumps({
            'id': f'retry-{round_num}',
            'label': f'🔄 Round {round_num}: Still failing (exit code {rc})',
            'status': 'error',
        }))
        _bus.emit('error_text', stderr_text[-3000:])

        # Check if this is the same error repeating
        if stderr_text.strip() == prev_error.strip() and prev_error:
            _bus.emit('log', '⚠ Same error as last round — the installed packages did not help.')
        prev_error = stderr_text

        # Re-bind status server for next round
        status_server = _start_status_server(host, port)
        if status_server is None:
            # Port stuck — wait a moment and retry
            time.sleep(2)
            status_server = _start_status_server(host, port)

    # Exhausted all rounds
    _bus.emit('done', json.dumps({
        'success': False,
        'reason': f'Exhausted {MAX_REPAIR_ROUNDS} repair rounds. Manual intervention needed.',
    }))
    print(f'[bootstrap] ❌ Gave up after {MAX_REPAIR_ROUNDS} rounds.', file=sys.stderr)
    _keep_alive_until_interrupt(status_server)


def _keep_alive_until_interrupt(server: http.server.HTTPServer | None):
    """Block until Ctrl+C or restart request from the API config form."""
    global _restart_requested
    if server is None:
        return
    print('[bootstrap] Status page still running. Press Ctrl+C to exit.', file=sys.stderr)
    try:
        while True:
            if _restart_requested:
                _restart_requested = False
                print('[bootstrap] \U0001f504 Restart requested via API config form.',
                      file=sys.stderr)
                _stop_status_server(server)
                # Re-load .env so _get_config() picks up the new keys
                _load_dotenv()
                # Reset the event bus so the next status page is clean
                global _bus
                _bus = EventBus()
                # Re-enter the main bootstrap flow
                main()
                return
            time.sleep(1)
    except KeyboardInterrupt:
        _stop_status_server(server)


if __name__ == '__main__':
    main()
