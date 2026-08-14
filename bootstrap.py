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
import ipaddress
import json
import os
import queue
import re
import signal
import socket
import subprocess
import sys
import tempfile
import textwrap
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

# ══════════════════════════════════════════════════════════
#  Configuration (mirrors server.py / lib/__init__.py)
# ══════════════════════════════════════════════════════════

BASE_DIR = os.path.dirname(os.path.abspath(__file__))


def _legacy_pg_runtime_requested() -> bool:
    """Compatibility name for the exact PostgreSQL backend selector."""
    backend = (os.environ.get('TOFU_DB_BACKEND') or 'sqlite').strip().lower()
    if backend not in {'sqlite', 'postgres'}:
        raise RuntimeError('TOFU_DB_BACKEND must be exactly sqlite or postgres')
    return backend == 'postgres'


# ══════════════════════════════════════════════════════════
#  Auto-activate Tofu's conda env via .tofu_env.json marker
# ══════════════════════════════════════════════════════════
# Mirror of the guard at the top of server.py — runs BEFORE we touch any
# pip / conda / subprocess logic so the rest of bootstrap.py operates
# inside the right interpreter (so subprocess [sys.executable, 'server.py']
# correctly inherits the env's python).
def _tofu_export_env_native_paths(env_prefix, backend, env_name=None):
    """Put the env's lib/ + bin/ on the search paths for CHILD processes.

    The headless-Chromium half (LD_LIBRARY_PATH + fontconfig) is delegated to
    chromium_env.ensure_chromium_env() — the single source of truth shared with
    server.py, tests/conftest.py and lib/motion_video. It resolves from
    sys.prefix rather than from this marker, so it also works on a fresh clone /
    exported bundle where no .tofu_env.json exists. chromium_env is stdlib-only
    BY CONTRACT precisely so bootstrap.py — whose job is to run when deps are
    still missing — can import it safely.

    Must run even when we are ALREADY in the env, since that path spawns
    Chromium too. Idempotent.
    """
    try:
        from chromium_env import ensure_chromium_env
        ensure_chromium_env(env_prefix=env_prefix)
    except Exception as e:
        sys.stderr.write(f'[bootstrap.py] chromium env setup skipped: {e}\n')

    if not env_prefix or not os.path.isdir(env_prefix):
        return
    env_bin = os.path.join(env_prefix, 'bin')
    if os.path.isdir(env_bin):
        _cur = os.environ.get('PATH', '')
        if env_bin not in _cur.split(os.pathsep):
            os.environ['PATH'] = (env_bin + os.pathsep + _cur) if _cur else env_bin
    # A uv venv (backend='uv') is not a conda env — don't set CONDA_PREFIX,
    # or _running_in_conda_env() below misfires and routes the pip fallback
    # down the conda-forge branch.
    if backend != 'uv':
        os.environ.setdefault('CONDA_PREFIX', env_prefix)
        if env_name:
            os.environ.setdefault('CONDA_DEFAULT_ENV', env_name)


def _tofu_maybe_reexec_into_env():
    marker = os.path.join(BASE_DIR, '.tofu_env.json')
    if not os.path.isfile(marker):
        # No marker — nothing to re-exec into, but Chromium still needs its GUI
        # libs + fonts, which chromium_env resolves from sys.prefix.
        _tofu_export_env_native_paths('', '')
        return
    try:
        with open(marker, 'r', encoding='utf-8') as f:
            cfg = json.load(f)
    except Exception as e:
        sys.stderr.write(
            f'[bootstrap.py] Could not read .tofu_env.json ({e}) — '
            f'continuing with current python.\n')
        return
    target_py = cfg.get('python') or ''
    env_prefix = cfg.get('env_prefix') or ''
    backend = cfg.get('backend') or ''
    if not target_py or not os.access(target_py, os.X_OK):
        return
    # Prefer a prefix check over a bare interpreter-path compare: a uv venv's
    # bin/python symlinks to a base CPython, so realpath(target_py) can equal
    # realpath(sys.executable) while the venv's site-packages are NOT active.
    # Comparing sys.prefix to env_prefix catches that (falls back to the
    # interpreter-path compare when env_prefix is absent, e.g. conda markers).
    same = False
    if env_prefix:
        try:
            same = (os.path.realpath(sys.prefix) == os.path.realpath(env_prefix))
        except OSError:
            same = (sys.prefix == env_prefix)
    else:
        try:
            same = os.path.realpath(target_py) == os.path.realpath(sys.executable)
        except OSError:
            same = (target_py == sys.executable)
    # Export BEFORE the early return — see server.py's twin helper. A direct
    # `python bootstrap.py` with the env interpreter takes this return, and
    # every Chromium it later spawns would otherwise miss $env_prefix/lib.
    _tofu_export_env_native_paths(env_prefix, backend, cfg.get('env_name'))
    if same:
        return
    if os.environ.get('_TOFU_ENV_REEXEC') == '1':
        sys.stderr.write(
            '\033[33m[bootstrap.py] WARNING: _TOFU_ENV_REEXEC=1 was inherited '
            'from your shell, but the current python\n'
            f'  ({sys.executable})\n'
            '  is NOT the env python recorded in .tofu_env.json\n'
            f'  ({target_py}).\n'
            '  Run:  unset _TOFU_ENV_REEXEC _TOFU_VIA_BOOTSTRAP\n'
            '  Overriding the leaked guard and re-execing into the env python now.\033[0m\n')
        sys.stderr.flush()
    os.environ['_TOFU_ENV_REEXEC'] = '1'
    sys.stderr.write(f'[bootstrap.py] Re-exec into Tofu env python: {target_py}\n')
    sys.stderr.flush()
    try:
        os.execv(target_py, [target_py, *sys.argv])
    except OSError as e:
        sys.stderr.write(f'[bootstrap.py] os.execv failed: {e}\n')
        os.environ.pop('_TOFU_ENV_REEXEC', None)


_tofu_maybe_reexec_into_env()
MAX_REPAIR_ROUNDS = 10       # give up after this many install→retry cycles
PIP_TIMEOUT = 300            # per-package install timeout
# Packages that should never be auto-installed (security / system-level)
_INSTALL_BLOCKLIST = frozenset({
    'python', 'python3', 'gcc', 'g++', 'make', 'cmake', 'apt', 'yum',
    'brew', 'sudo', 'pip', 'setuptools', 'wheel',
})


# ══════════════════════════════════════════════════════════
#  Provider templates — reused from Settings UI
# ══════════════════════════════════════════════════════════
# The canonical list in static/js/settings.js is ~200 providers and far
# too large to inline here. We ship a curated subset of the most common
# public providers so the bootstrap "Configure API" flow has a usable
# picker even when NO static/provider_templates/*.json file exists.
#
# At runtime we ALSO merge in static/provider_templates/*.json (same
# mechanism as the main Settings UI) so corp-gateway templates like
# meituan.json automatically appear in the bootstrap picker too.
#
# Keep this list short + curated — the goal is unblocking installation,
# not replicating all of Settings.
_BUILTIN_PROVIDER_TEMPLATES = [
    {'key': 'openai', 'brand': 'openai', 'category': 'official',
     'name': 'OpenAI',
     'base_url': 'https://api.openai.com/v1',
     'protocol': 'responses',
     'responses_profile': 'openai',
     'models': [
         {'model_id': 'gpt-5.6',       'capabilities': ['text', 'vision', 'thinking']},
         {'model_id': 'gpt-5.6-sol',   'capabilities': ['text', 'vision', 'thinking']},
         {'model_id': 'gpt-5.6-terra', 'capabilities': ['text', 'vision', 'thinking', 'cheap']},
         {'model_id': 'gpt-5.6-luna',  'capabilities': ['text', 'vision', 'thinking', 'cheap']},
         {'model_id': 'gpt-5.4',      'capabilities': ['text', 'vision', 'thinking']},
         {'model_id': 'o3',           'capabilities': ['text', 'vision', 'thinking']},
         {'model_id': 'o4-mini',      'capabilities': ['text', 'vision', 'thinking', 'cheap']},
         {'model_id': 'gpt-4.1',      'capabilities': ['text', 'vision']},
         {'model_id': 'gpt-4.1-mini', 'capabilities': ['text', 'vision', 'cheap']},
     ]},
    {'key': 'anthropic', 'brand': 'claude', 'category': 'official',
     'name': 'Anthropic',
     'base_url': 'https://api.anthropic.com/v1',
     'models': [
         {'model_id': 'fable-5',           'capabilities': ['text', 'vision', 'thinking']},
         {'model_id': 'claude-opus-4-8',   'capabilities': ['text', 'vision', 'thinking']},
         {'model_id': 'claude-opus-4-7',   'capabilities': ['text', 'vision', 'thinking']},
         {'model_id': 'claude-sonnet-4-6', 'capabilities': ['text', 'vision', 'thinking']},
         {'model_id': 'claude-haiku-4-5',  'capabilities': ['text', 'vision', 'cheap']},
     ]},
    {'key': 'deepseek', 'brand': 'deepseek', 'category': 'official',
     'name': 'DeepSeek',
     'base_url': 'https://api.deepseek.com',
     'models': [
         {'model_id': 'deepseek-v4-pro',   'capabilities': ['text', 'thinking', 'cheap']},
         {'model_id': 'deepseek-v4-flash', 'capabilities': ['text', 'thinking', 'cheap']},
     ]},
    {'key': 'glm', 'brand': 'glm', 'category': 'official',
     'name': 'GLM (Zhipu AI)',
     'base_url': 'https://open.bigmodel.cn/api/paas/v4',
     'models': [
         {'model_id': 'glm-5.2',       'capabilities': ['text', 'thinking']},
         {'model_id': 'glm-5.1',       'capabilities': ['text', 'thinking']},
         {'model_id': 'glm-4.7',       'capabilities': ['text', 'thinking', 'cheap']},
         {'model_id': 'glm-4.5-flash', 'capabilities': ['text', 'cheap']},
     ]},
    {'key': 'kimi', 'brand': 'kimi', 'category': 'official',
     'name': 'Moonshot (Kimi)',
     'base_url': 'https://api.moonshot.ai/v1',
     'models': [
         {'model_id': 'kimi-k3',          'capabilities': ['text', 'vision', 'video', 'thinking', 'cheap']},
         {'model_id': 'kimi-k2.6',        'capabilities': ['text', 'vision', 'thinking', 'cheap']},
         {'model_id': 'kimi-k2-thinking', 'capabilities': ['text', 'thinking', 'cheap']},
     ]},
    {'key': 'qwen', 'brand': 'qwen', 'category': 'official',
     'name': 'Qwen (DashScope)',
     'base_url': 'https://dashscope.aliyuncs.com/compatible-mode/v1',
     'models': [
         {'model_id': 'qwen3-max',     'capabilities': ['text', 'thinking', 'cheap']},
         {'model_id': 'qwen-plus',     'capabilities': ['text', 'thinking', 'cheap']},
         {'model_id': 'qwen-flash',    'capabilities': ['text', 'cheap']},
     ]},
    {'key': 'gemini', 'brand': 'gemini', 'category': 'official',
     'name': 'Google Gemini',
     'base_url': 'https://generativelanguage.googleapis.com/v1beta/openai',
     'models': [
         {'model_id': 'gemini-3.1-pro-preview',        'capabilities': ['text', 'vision', 'thinking']},
         {'model_id': 'gemini-2.5-pro',                 'capabilities': ['text', 'vision', 'thinking', 'cheap']},
         {'model_id': 'gemini-2.5-flash',               'capabilities': ['text', 'vision', 'cheap']},
         {'model_id': 'gemini-3.1-flash-lite-preview',  'capabilities': ['text', 'cheap']},
     ]},
    {'key': 'xai', 'brand': 'grok', 'category': 'official',
     'name': 'xAI (Grok)',
     'base_url': 'https://api.x.ai/v1',
     'models': [
         {'model_id': 'grok-4.20',     'capabilities': ['text', 'vision', 'thinking', 'cheap']},
         {'model_id': 'grok-4.1-mini', 'capabilities': ['text', 'vision', 'cheap']},
     ]},
    {'key': 'doubao', 'brand': 'doubao', 'category': 'official',
     'name': 'Doubao (Volcengine)',
     'base_url': 'https://ark.cn-beijing.volces.com/api/v3',
     'models': [
         {'model_id': 'doubao-seed-2-0-pro-260215',  'capabilities': ['text', 'vision', 'thinking', 'cheap']},
         {'model_id': 'doubao-seed-2-0-lite-260215', 'capabilities': ['text', 'cheap']},
     ]},
    {'key': 'bedrock', 'brand': 'bedrock', 'category': 'official',
     'name': 'Amazon Bedrock (OpenAI-compat)',
     'base_url': 'https://bedrock-runtime.us-east-1.amazonaws.com/openai/v1',
     'models': [
         {'model_id': 'us.anthropic.claude-opus-4-7-v1:0',   'capabilities': ['text', 'vision', 'thinking']},
         {'model_id': 'us.anthropic.claude-sonnet-4-6-v1:0', 'capabilities': ['text', 'vision', 'thinking']},
         {'model_id': 'us.anthropic.claude-haiku-4-5-v1:0',  'capabilities': ['text', 'vision', 'cheap']},
     ]},
    {'key': 'openrouter', 'brand': 'openrouter', 'category': 'relay',
     'name': 'OpenRouter',
     'base_url': 'https://openrouter.ai/api/v1',
     'models': [
         {'model_id': 'anthropic/claude-sonnet-4.6',    'capabilities': ['text', 'vision', 'thinking']},
         {'model_id': 'openai/gpt-5.4',                 'capabilities': ['text', 'vision', 'thinking']},
         {'model_id': 'google/gemini-3.1-pro-preview',  'capabilities': ['text', 'vision', 'thinking', 'cheap']},
         {'model_id': 'deepseek/deepseek-chat',         'capabilities': ['text', 'cheap']},
     ]},
    {'key': 'custom', 'brand': 'custom', 'category': 'custom',
     'name': 'Custom (OpenAI-compatible)',
     'base_url': '',
     'models': [
         {'model_id': '', 'capabilities': ['text']},
     ]},
]


def _load_provider_templates() -> list:
    """Return builtin templates merged with static/provider_templates/*.json.

    Extras from disk override builtins on key conflict (so e.g. meituan.json
    replaces an inline stub of the same key). Called on every HTTP request
    so freshly-dropped template files appear without a restart.
    """
    out: list = [dict(t) for t in _BUILTIN_PROVIDER_TEMPLATES]
    extras_dir = os.path.join(BASE_DIR, 'static', 'provider_templates')
    if not os.path.isdir(extras_dir):
        return out
    seen = {t['key']: i for i, t in enumerate(out)}
    for fname in sorted(os.listdir(extras_dir)):
        if not fname.endswith('.json'):
            continue
        fpath = os.path.join(extras_dir, fname)
        try:
            with open(fpath, 'r', encoding='utf-8') as f:
                tpl = json.load(f)
        except Exception as e:
            sys.stderr.write(
                f'[bootstrap] Could not read template {fname}: {e}\n')
            continue
        if not (isinstance(tpl, dict) and tpl.get('key') and tpl.get('models')):
            continue
        key = tpl['key']
        if key in seen:
            out[seen[key]] = tpl   # override builtin
        else:
            out.append(tpl)
            seen[key] = len(out) - 1
    return out


def _bootstrap_infer_capabilities(model_id: str) -> list:
    """Small stdlib-only fallback for live IDs absent from bundled metadata."""
    mid = (model_id or '').lower()
    if any(x in mid for x in ('embedding', 'embed-', 'text-embedding')):
        return ['embedding']
    if any(x in mid for x in ('image-gen', 'image_generation', 'gpt-image',
                              'dall-e', 'imagen')):
        return ['image_gen']
    if any(x in mid for x in ('transcribe', 'whisper', 'speech-to-text')):
        return ['transcription']
    caps = ['text']
    if any(x in mid for x in ('vision', '-vl', 'vl-', 'omni', 'gemini',
                              'claude', 'gpt-4o', 'gpt-4.1', 'gpt-5')):
        caps.append('vision')
    if any(x in mid for x in ('thinking', 'reasoner', 'reasoning', 'deepseek-r1',
                              'qwq', 'o1', 'o3', 'o4', 'gpt-5', 'claude')):
        caps.append('thinking')
    if any(x in mid for x in ('mini', 'nano', 'flash', 'lite', 'haiku',
                              'turbo', 'small')):
        caps.append('cheap')
    return caps


def _bootstrap_discover_models(base_url: str, api_key: str,
                               templates: list | None = None,
                               timeout: int = 10) -> list:
    """Fetch an authenticated OpenAI-compatible catalogue using stdlib only.

    Bootstrap exists precisely for environments whose third-party packages
    are broken, so importing the application's normal discovery stack here is
    not an option. Fail-soft: callers retain the selected template model when
    this endpoint is unavailable.
    """
    if not base_url or not api_key:
        return []
    url = base_url.rstrip('/') + '/models'
    try:
        parsed = urllib.parse.urlparse(url)
        if parsed.scheme not in ('http', 'https') or not parsed.hostname:
            raise ValueError('model endpoint must be an http(s) URL with a hostname')
        allow_hosts = {
            h.strip().lower()
            for h in os.environ.get('TOFU_BYO_ALLOW_HOSTS', '').split(',')
            if h.strip()
        }
        if parsed.hostname.lower() not in allow_hosts:
            infos = socket.getaddrinfo(
                parsed.hostname, parsed.port or None, proto=socket.IPPROTO_TCP)
            if not infos:
                raise ValueError('model endpoint DNS returned no addresses')
            for info in infos:
                ip = ipaddress.ip_address(info[4][0])
                ip = getattr(ip, 'ipv4_mapped', None) or ip
                blocked = (ip.is_link_local or ip.is_multicast
                           or ip.is_reserved or ip.is_unspecified)
                block_loopback = os.environ.get(
                    'TOFU_BYO_BLOCK_LOOPBACK', '').strip().lower() in (
                        '1', 'true', 'yes', 'on')
                block_private = os.environ.get(
                    'TOFU_BYO_BLOCK_PRIVATE', '').strip().lower() in (
                        '1', 'true', 'yes', 'on')
                if (blocked or (block_loopback and ip.is_loopback)
                        or (block_private and ip.is_private
                            and not ip.is_loopback)):
                    raise ValueError(
                        f'model endpoint resolves to blocked address {ip}')
    except (OSError, ValueError) as e:
        sys.stderr.write(f'[bootstrap] model discovery blocked for {base_url}: {e}\n')
        return []

    req = urllib.request.Request(url, headers={
        'Authorization': 'Bearer ' + api_key,
        'User-Agent': 'Tofu-Bootstrap/1.0',
        'Accept': 'application/json',
    })
    try:
        # Do not follow redirects: a public-looking provider URL redirecting
        # to a link-local metadata service would bypass the check above.
        class _NoRedirect(urllib.request.HTTPRedirectHandler):
            def redirect_request(self, request, fp, code, msg, headers, newurl):
                return None

        opener = urllib.request.build_opener(_NoRedirect)
        with opener.open(req, timeout=timeout) as resp:
            if getattr(resp, 'status', 200) >= 400:
                return []
            raw = resp.read(4 * 1024 * 1024 + 1)
    except (urllib.error.URLError, TimeoutError, OSError, ValueError) as e:
        sys.stderr.write(f'[bootstrap] model discovery failed for {base_url}: {e}\n')
        return []
    if len(raw) > 4 * 1024 * 1024:
        sys.stderr.write('[bootstrap] model discovery response exceeded 4 MiB\n')
        return []
    try:
        data = json.loads(raw.decode('utf-8'))
    except (UnicodeDecodeError, json.JSONDecodeError) as e:
        sys.stderr.write(f'[bootstrap] invalid model catalogue JSON: {e}\n')
        return []
    rows = data.get('data') if isinstance(data, dict) else None
    if not isinstance(rows, list):
        return []

    metadata = {}
    catalog_templates = (templates if templates is not None
                         else _load_provider_templates())
    norm_base = base_url.rstrip('/')
    matching_templates = [
        tpl for tpl in catalog_templates
        if str(tpl.get('base_url') or '').rstrip('/') == norm_base
    ]
    for tpl in matching_templates:
        for model in (tpl.get('models') or []):
            if isinstance(model, dict) and model.get('model_id'):
                metadata.setdefault(model['model_id'], model)
    result = []
    seen = set()
    for raw_model in rows:
        if not isinstance(raw_model, dict):
            continue
        model_id = str(raw_model.get('id') or '').strip()
        if (not model_id or model_id in seen
                or model_id.startswith(('system-', 'ft:', 'ft-'))):
            continue
        seen.add(model_id)
        known = metadata.get(model_id) or {}
        caps = list(known.get('capabilities') or
                    _bootstrap_infer_capabilities(model_id))
        result.append({
            'model_id': model_id,
            'aliases': list(known.get('aliases') or []),
            'capabilities': caps,
            'rpm': int(known.get('rpm') or 30),
            'cost': float(known.get('cost') or 0.01),
            'thinking_default': 'thinking' in caps,
            'catalog_managed': True,
            'catalog_source': 'provider',
        })
    result.sort(key=lambda m: m['model_id'].casefold())
    return result


def _bootstrap_choose_model(models: list, requested: str = '') -> str:
    """Keep a valid selection; otherwise choose a cheap chat model."""
    chat = [m for m in (models or [])
            if 'text' in set(m.get('capabilities') or [])]
    ids = {m.get('model_id') for m in chat}
    if requested in ids:
        return requested
    for model in chat:
        if 'cheap' in set(model.get('capabilities') or []):
            return model['model_id']
    return chat[0]['model_id'] if chat else ''


def _bootstrap_template_models(base_url: str, templates: list) -> list:
    """Convert the matching bundled template into persisted bootstrap rows."""
    norm = (base_url or '').rstrip('/')
    template = next((tpl for tpl in (templates or [])
                     if str(tpl.get('base_url') or '').rstrip('/') == norm), {})
    result = []
    for raw in (template.get('models') or []):
        if not isinstance(raw, dict) or not raw.get('model_id'):
            continue
        row = dict(raw)
        caps = list(row.get('capabilities') or
                    _bootstrap_infer_capabilities(row['model_id']))
        row['capabilities'] = caps
        row.setdefault('aliases', [])
        row.setdefault('rpm', 30)
        row.setdefault('cost', 0.01)
        row.setdefault('thinking_default', 'thinking' in caps)
        row['catalog_managed'] = True
        row['catalog_source'] = 'template'
        result.append(row)
    return result


def _bootstrap_data_root() -> str:
    """Stdlib twin of lib.runtime_paths.data_root for pre-dependency setup."""
    explicit = os.environ.get('TOFU_DATA_DIR', '').strip()
    if explicit:
        explicit = os.path.abspath(os.path.expanduser(explicit))
        return (explicit if os.path.basename(explicit) == 'data'
                else os.path.join(explicit, 'data'))

    def _per_user_data() -> str:
        if sys.platform.startswith('win'):
            base = os.environ.get('LOCALAPPDATA') or os.path.expanduser('~')
            return os.path.join(base, 'Tofu', 'data')
        if sys.platform == 'darwin':
            return os.path.join(os.path.expanduser('~'), 'Library',
                                'Application Support', 'Tofu', 'data')
        base = os.environ.get('XDG_DATA_HOME') or os.path.join(
            os.path.expanduser('~'), '.local', 'share')
        return os.path.join(base, 'Tofu', 'data')

    if getattr(sys, 'frozen', False):
        exe_base = os.path.dirname(sys.executable)
        try:
            os.makedirs(exe_base, exist_ok=True)
            fd, probe = tempfile.mkstemp(prefix='.tofu-write-probe-', dir=exe_base)
            os.close(fd)
            os.unlink(probe)
            return os.path.join(exe_base, 'data')
        except OSError:
            return _per_user_data()

    intree = os.path.join(BASE_DIR, 'data')
    layout = os.environ.get('TOFU_DATA_LAYOUT', 'auto').strip().lower()
    if layout not in ('auto', 'intree', 'xdg'):
        layout = 'auto'
    try:
        populated = os.path.isdir(intree) and bool(os.listdir(intree))
    except OSError:
        populated = False
    if layout == 'intree' or (layout == 'auto' and populated):
        return intree
    return _per_user_data()


def _bootstrap_persist_provider(base_url: str, api_key: str, models: list,
                                templates: list | None = None,
                                default_model: str = '') -> None:
    """Merge the bootstrap catalogue into server_config.json atomically."""
    if not models:
        return
    templates = (templates if templates is not None
                 else _load_provider_templates())
    norm = base_url.rstrip('/')
    template = next((t for t in templates
                     if str(t.get('base_url') or '').rstrip('/') == norm), {})
    config_path = os.path.join(_bootstrap_data_root(), 'config',
                               'server_config.json')
    try:
        with open(config_path, 'r', encoding='utf-8') as f:
            config = json.load(f)
        if not isinstance(config, dict):
            config = {}
    except (FileNotFoundError, OSError, json.JSONDecodeError):
        config = {}
    providers = config.get('providers')
    if not isinstance(providers, list):
        providers = config['providers'] = []
    provider = next((p for p in providers if isinstance(p, dict)
                     and str(p.get('base_url') or '').rstrip('/') == norm
                     and p.get('oauth') not in ('claude', 'codex')
                     and not isinstance(p.get('adapter'), dict)
                     and not str(p.get('id') or '').startswith('adapter_')),
                    None)
    if provider is None:
        safe_key = re.sub(r'[^a-z0-9_]+', '_',
                          str(template.get('key') or 'bootstrap').lower())
        base_id = (safe_key or 'bootstrap') + '_bootstrap'
        used_ids = {p.get('id') for p in providers if isinstance(p, dict)}
        provider_id = base_id
        suffix = 2
        while provider_id in used_ids:
            provider_id = f'{base_id}_{suffix}'
            suffix += 1
        provider = {'id': provider_id}
        providers.append(provider)
    previous_keys = [k for k in (provider.get('api_keys') or [])
                     if isinstance(k, str) and k]
    catalog_models = list(models)
    live_ids = {m.get('model_id') for m in catalog_models if isinstance(m, dict)}
    for old_model in (provider.get('models') or []):
        if (isinstance(old_model, dict) and old_model.get('model_id')
                and old_model.get('catalog_pinned') is True
                and old_model['model_id'] not in live_ids):
            catalog_models.append(old_model)
            live_ids.add(old_model['model_id'])
    provider.update({
        'name': template.get('name') or provider.get('name') or 'Bootstrap Provider',
        'brand': template.get('brand') or provider.get('brand') or 'generic',
        'base_url': base_url,
        'api_keys': list(dict.fromkeys([api_key] + previous_keys)),
        'enabled': True,
        'models': catalog_models,
        'model_catalog_sync': {'mode': 'auto'},
    })
    for field in ('balance_url', 'protocol', 'responses_profile', 'thinking_format',
                  'extra_headers', 'faces'):
        if template.get(field) and not provider.get(field):
            provider[field] = template[field]
    if default_model:
        config.setdefault('presets', {})['opus'] = default_model
        config.setdefault('models', {})['LLM_MODEL'] = default_model
        config.setdefault('model_defaults', {})['default_model'] = default_model

    parent = os.path.dirname(config_path)
    os.makedirs(parent, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(prefix='.bootstrap-config-', suffix='.tmp',
                                    dir=parent)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as f:
            json.dump(config, f, ensure_ascii=False, indent=2)
            f.write('\n')
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, config_path)
    finally:
        try:
            if os.path.exists(tmp_path):
                os.unlink(tmp_path)
        except OSError:
            pass


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
          (e.g. "python-dateutil" not "dateutil").
        - If a ModuleNotFoundError names a module like "foo.bar",
          the pip package is usually just "foo" — but use your knowledge
          to map correctly (e.g. module "cv2" → pip "opencv-python").
        - When you see a missing package, suggest only dependencies actually
          required by the failing native Quart stack.
        - Never suggest system packages (apt/yum), only pip packages.
        - SQLite and PostgreSQL are equal Storage Sidecar backends. SQLite is
          default; suggest PostgreSQL packages only when the exact selector is
          TOFU_DB_BACKEND=postgres. Never suggest automatic backend fallback.

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

# Map requirements.txt line → conda-forge package spec. Used when we detect
# we're running inside a conda env (install.sh created one).
# conda-forge builds link against an older sysroot glibc (2.17) so they work
# on CentOS-7-class hosts where pip's manylinux wheels crash with
# "GLIBC_2.25 not found" (classic lxml failure mode).
_CONDA_PYTHON_DEPS = [
    # ── Boot-critical: the server cannot start without these ──
    # quart + hypercorn are the ASGI stack (server.py).
    'quart>=0.20',
    'hypercorn>=0.17',
    # orjson is REQUIRED (not optional) — the SSE state snapshot in
    # routes/chat.py depends on it to avoid the event-loop stall (see
    # requirements.txt). sqlalchemy is imported UNCONDITIONALLY in the chat
    # hot-path (lib/chat/persistence.py → _core_schema), so its absence breaks
    # chat send, not just an optional feature.
    'orjson>=3.9',
    'sqlalchemy>=2.0',
    'requests>=2.31',
    'psutil>=5.9',
    'trafilatura>=1.6',
    'playwright>=1.40',
    'pillow>=10.0',
    'python-pptx>=0.6.21',
    'lxml>=5.3',
    'lxml_html_clean>=0.4',
    # Bounded on BOTH sides — see requirements.txt. Tofu's client speaks the
    # v2 API (>=2,<3); vendored servers pin their own mcp in their own
    # isolated envs. This list feeds the PRE-BOOT installer, so a spec that
    # disagrees with requirements.txt here builds a different client before
    # the app has even started.
    'mcp>=2,<3',
]

# Boot-critical packages the conda-forge repair path MUST cover — asserted by
# tests/test_bootstrap_conda_deps_coverage.py so this list can never again
# silently drift below what server boot + the chat hot-path require. Names are
# the bare (version-stripped, lower-cased) package names.
_CRITICAL_BOOT_PACKAGES = ('quart', 'hypercorn', 'orjson', 'sqlalchemy')


def _running_in_conda_env() -> bool:
    """True when the current Python is running inside a conda env."""
    # CONDA_PREFIX is set when a conda env is activated; also set by
    # install.sh launching via exec into the env's python.
    prefix = os.environ.get('CONDA_PREFIX', '')
    if prefix and os.path.isdir(prefix):
        return True
    # Fallback: site-packages path contains 'conda' / 'miniforge' / 'miniconda'
    exe = sys.executable or ''
    lowered = exe.lower()
    return any(tok in lowered for tok in ('miniforge', 'miniconda', 'anaconda', '/conda/', '\\conda\\'))


def _find_conda_exe() -> str | None:
    """Locate the conda executable reachable from the current env."""
    import shutil as _sh
    # Prefer the one that created the current env
    for env_name in ('CONDA_EXE', 'MAMBA_EXE'):
        val = os.environ.get(env_name)
        if val and os.path.isfile(val):
            return val
    # PATH lookup
    for name in ('conda', 'mamba'):
        path = _sh.which(name)
        if path:
            return path
    # Guess from $CONDA_PREFIX → parent base env
    prefix = os.environ.get('CONDA_PREFIX', '')
    if prefix:
        # e.g. ~/miniforge3/envs/tofu → ~/miniforge3/bin/conda
        for up in (prefix, os.path.dirname(os.path.dirname(prefix))):
            for rel in ('bin/conda', 'Scripts/conda.exe'):
                cand = os.path.join(up, rel)
                if os.path.isfile(cand):
                    return cand
    return None


def _try_conda_install_deps() -> bool:
    """Install dependencies via `conda install -c conda-forge` into the
    current env, heal pre-existing broken pip wheels first.

    This is the preferred repair path when we're inside a conda env —
    conda-forge avoids the glibc-mismatch issue that breaks pip's
    manylinux lxml wheel on older hosts (CentOS 7 / glibc 2.17).

    Returns True on success.
    """
    conda = _find_conda_exe()
    if not conda:
        _bus.emit('log', 'conda not found — falling back to pip path')
        return False

    # Figure out the env name. If we can, install by name so it works even
    # when CONDA_PREFIX points at a path that's awkward to pass to conda.
    env_name = os.environ.get('CONDA_DEFAULT_ENV', '') or 'base'
    env_prefix = os.environ.get('CONDA_PREFIX', '')

    _bus.emit('phase', json.dumps({
        'id': 'conda-deps',
        'label': f'🐍 Detected conda env ({env_name}) — installing deps from conda-forge',
        'status': 'active',
        'detail': 'conda-forge wheels link against older glibc (CentOS 7-compatible). '
                  'Pip manylinux wheels of lxml often require GLIBC_2.25+.',
    }))

    # ── Step 1: purge any pip-installed copies that would shadow conda-forge ──
    _bus.emit('log', '🧹 Purging pip-installed deps that would shadow conda-forge…')
    pip_list = subprocess.run(
        [sys.executable, '-m', 'pip', 'list', '--format=freeze'],
        capture_output=True, text=True, cwd=BASE_DIR,
    )
    installed_pip = set()
    if pip_list.returncode == 0:
        for line in pip_list.stdout.splitlines():
            name = line.split('==', 1)[0].strip().lower()
            if name:
                installed_pip.add(name)

    # Extract bare package names from _CONDA_PYTHON_DEPS (strip version specifiers)
    bare_names = []
    for spec in _CONDA_PYTHON_DEPS:
        # e.g. "python-dateutil>=2.9" → "python-dateutil"
        base = re.split(r'[<>=!~\[]', spec, 1)[0].strip()
        bare_names.append(base)

    to_uninstall = sorted({n for n in bare_names if n.lower() in installed_pip})
    if to_uninstall:
        _bus.emit('log', f'Removing pip copies: {to_uninstall}')
        uninst = subprocess.run(
            [sys.executable, '-m', 'pip', 'uninstall', '-y', *to_uninstall],
            capture_output=True, text=True, cwd=BASE_DIR,
        )
        for line in (uninst.stdout + uninst.stderr).splitlines():
            _bus.emit('pip_output', line)

    # ── Step 2: refresh conda itself (outdated conda → solver hangs) ──
    _bus.emit('log', '🔄 Updating conda itself (outdated conda causes solver issues)…')
    upd = subprocess.run(
        [conda, 'update', '-n', 'base', '-c', 'conda-forge',
         '--override-channels', '-y', 'conda'],
        capture_output=True, text=True, cwd=BASE_DIR,
    )
    for line in (upd.stdout + upd.stderr).splitlines()[-10:]:
        _bus.emit('pip_output', line)
    if upd.returncode != 0:
        _bus.emit('log', '⚠ conda self-update failed — continuing with existing version')

    # Install libmamba solver (10x faster, avoids classic solver hangs)
    lm = subprocess.run(
        [conda, 'install', '-n', 'base', '-c', 'conda-forge',
         '--override-channels', '-y', 'conda-libmamba-solver'],
        capture_output=True, text=True, cwd=BASE_DIR,
    )
    if lm.returncode == 0:
        subprocess.run([conda, 'config', '--set', 'solver', 'libmamba'],
                       capture_output=True, text=True, cwd=BASE_DIR)
        _bus.emit('log', '✓ libmamba solver active')

    # ── Step 3: conda install from conda-forge ──
    if env_prefix and os.path.isdir(env_prefix):
        target = ['-p', env_prefix]
    else:
        target = ['-n', env_name]

    # --force-reinstall handles the case where pip previously dropped an
    # incompatible manylinux wheel over conda's files. Without it, conda's
    # cached metadata says the package is satisfied and it no-ops.
    cmd = [conda, 'install', *target, '-c', 'conda-forge',
           '--override-channels', '-y',
           '--force-reinstall', *_CONDA_PYTHON_DEPS]
    _bus.emit('log', f'$ {" ".join(cmd)}')

    try:
        proc = subprocess.Popen(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, bufsize=1, cwd=BASE_DIR)
    except Exception as e:
        _bus.emit('log', f'Failed to run conda: {e}')
        _bus.emit('phase', json.dumps({
            'id': 'conda-deps',
            'label': '🐍 conda failed to start',
            'status': 'error',
        }))
        return False

    for line in proc.stdout:
        line = line.rstrip('\n')
        _bus.emit('pip_output', line)

    proc.wait(timeout=600)  # conda can take a while, esp. first time

    if proc.returncode == 0:
        _bus.emit('log', '✅ conda install succeeded')
        _bus.emit('phase', json.dumps({
            'id': 'conda-deps',
            'label': '🐍 Dependencies installed from conda-forge',
            'status': 'done',
        }))
        return True

    _bus.emit('log', f'❌ conda install failed (exit code {proc.returncode})')
    _bus.emit('phase', json.dumps({
        'id': 'conda-deps',
        'label': '🐍 conda install failed',
        'status': 'error',
        'detail': f'Exit code {proc.returncode}. Falling back to pip…',
    }))
    return False


def _try_requirements_txt() -> bool:
    """Try to install all packages from requirements.txt.

    This is the fast path: if a requirements.txt exists, we can install
    everything from it without needing the LLM at all.  This is critical
    for freshly-exported projects where the LLM API keys haven't been
    configured yet.

    If the current Python is inside a conda env, we use conda-forge
    instead of pip — this avoids the glibc-mismatch trap where pip's
    manylinux wheels (esp. lxml) require a newer glibc than the host.

    Returns True if dependencies were successfully installed.
    """
    req_path = os.path.join(BASE_DIR, 'requirements.txt')
    if not os.path.isfile(req_path):
        return False

    # Prefer conda when we're inside a conda env — see _try_conda_install_deps
    # docstring for the glibc rationale.
    if _running_in_conda_env() and _try_conda_install_deps():
        return True

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
    """Install PostgreSQL only when that equal backend is explicitly selected."""
    if not _legacy_pg_runtime_requested():
        _bus.emit('log', 'PostgreSQL install skipped: the selected/default '
                         'Storage Sidecar backend is SQLite. Set exact '
                         'TOFU_DB_BACKEND=postgres to provision PostgreSQL.')
        return False
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

    cmd = [conda_bin, 'install', '-c', 'conda-forge', '--override-channels',
           '-y', 'postgresql>=18']
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

    # The only historical conda suggestion is PostgreSQL. Never let an LLM
    # pull it into the normal SQLite runtime; explicit migration windows retain
    # the old repair path.
    conda_ok = True
    if conda_pkgs:
        _bus.emit('log', f'Detected conda packages: {conda_pkgs}')
        conda_ok = _try_conda_install_postgresql()

    # Filter out blocked packages
    safe_pkgs = [p for p in pip_pkgs if p.lower() not in _INSTALL_BLOCKLIST]
    if not safe_pkgs and not conda_pkgs:
        return False, 'All suggested packages are in the blocklist.'
    if not safe_pkgs:
        return conda_ok, ('Legacy PostgreSQL package handled.' if conda_ok else
                          'PostgreSQL repair was not explicitly requested.')

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
            templates = _load_provider_templates()
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
            templates = _load_provider_templates()
            live_models = _bootstrap_discover_models(
                base_url, api_key, templates=templates)
            if live_models and not custom_model:
                model = _bootstrap_choose_model(live_models, model) or model

            persist_models = live_models or _bootstrap_template_models(
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
                caps = _bootstrap_infer_capabilities(model)
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
                    _bootstrap_persist_provider(
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
    env['_TOFU_VIA_BOOTSTRAP'] = '1'      # prevent server.py → bootstrap.py re-delegation loop
    env['TOFU_SERVER_WORKER'] = '1'        # bootstrap owns/tracks this foreground child
    env['TOFU_MANAGED_BY'] = 'bootstrap'
    env['BOOTSTRAP_LAUNCHER_PID'] = str(os.getpid())  # sentinel so server.py can tell a real bootstrap child from a leaked guard
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
    # — that's not a crash, it's intentional. 130 (SIGINT / a second-Ctrl+C
    # force-quit) and 143 (SIGTERM) are likewise deliberate stops, not crashes:
    # do NOT feed them into the LLM dependency-repair loop.
    if rc in (0, 130, 143):
        sys.exit(0)

    # Non-zero exit → crash.
    return False, stderr_text, rc


def _is_external_kill(rc: int) -> bool:
    """True when server.py died from SIGKILL (rc -9, or shell-style 137).

    SIGKILL is untrappable and leaves no traceback: feeding the empty stderr
    into the LLM dependency-repair loop would 'diagnose' nothing. The cause
    is almost always the container OOM killer (shared cgroup — see
    lib/cgroup_guard.py) or an external reaper. The right response is to
    record evidence and RESTART, not to repair dependencies.
    """
    return rc in (-9, 137)


def _log_external_kill(rc: int) -> None:
    """Durable evidence of a SIGKILL death — stderr + logs/watchdog.log."""
    import datetime
    line = ('%s [bootstrap] server.py SIGKILLed (exit %s) — almost always the '
            'container OOM killer (shared cgroup, zero swap). See '
            'logs/cgroup_pressure.log for the pressure curve; restarting.'
            % (datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S'), rc))
    print(line, file=sys.stderr)
    try:
        os.makedirs(os.path.join(BASE_DIR, 'logs'), exist_ok=True)
        with open(os.path.join(BASE_DIR, 'logs', 'watchdog.log'), 'a',
                  encoding='utf-8') as f:
            f.write(line + '\n')
    except OSError as e:
        print(f'[bootstrap] could not write watchdog.log: {e}', file=sys.stderr)


def _restart_after_external_kill(first_rc: int, max_relaunches: int = 5) -> None:
    """Relaunch server.py after SIGKILL deaths, with linear backoff.

    A SIGKILLed server is healthy code killed by the environment — restarting
    is the whole fix (same contract as supervisord autorestart=true, for
    users who launch via bootstrap). Gives up after max_relaunches
    consecutive kills so a pathological kill-loop cannot spin forever.
    Never returns on success (server runs forever / clean exit sys.exit()s
    inside _try_start_server). When the relaunched server dies of something
    OTHER than SIGKILL, RETURNS that death's ``(stderr_text, rc)`` so the
    caller can enter the repair flow with the real error.
    """
    rc = first_rc
    for attempt in range(1, max_relaunches + 1):
        _log_external_kill(rc)
        backoff = min(5 * attempt, 30)
        print(f'[bootstrap] 🔄 relaunching after SIGKILL '
              f'(attempt {attempt}/{max_relaunches}, backoff {backoff}s)…',
              file=sys.stderr)
        time.sleep(backoff)
        _, stderr_text, rc = _try_start_server()
        # On success / clean exit _try_start_server never returns.
        if _is_external_kill(rc):
            continue
        print(f'[bootstrap] ⚠ relaunched server crashed differently '
              f'(exit {rc}) — entering repair mode.', file=sys.stderr)
        return stderr_text, rc
    print(f'[bootstrap] ❌ server.py SIGKILLed {max_relaunches}× in a row — '
          f'giving up. The container is under sustained memory pressure; '
          f'see logs/cgroup_pressure.log and lib/cgroup_guard.py.',
          file=sys.stderr)
    sys.exit(137)


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


def _is_mypyc_error(stderr_text: str) -> bool:
    """Heuristic: does the error look like a broken mypyc compiled extension?

    Packages like charset-normalizer, black, and mypy ship mypyc-compiled
    .so/.pyd files.  When a user's Python version or platform doesn't match
    the compiled extension, the import fails with:
        No module named '<hash>__mypyc'
    or:
        partially initialized module '...' has no attribute 'md__mypyc'

    Fix: ``pip install --force-reinstall <package>`` to get a wheel that
    matches the current Python.
    """
    return bool(re.search(r"No module named '[0-9a-f]+__mypyc'", stderr_text)
                or '__mypyc' in stderr_text)


# Known packages that ship mypyc-compiled extensions.
# Keys are regex patterns matched against the stderr traceback to identify
# which pip package is broken.  Patterns use word boundaries to avoid
# false positives (e.g. '__mypyc' should NOT match the 'mypyc' entry).
_MYPYC_PACKAGE_PATTERNS: list[tuple[re.Pattern, str]] = [
    (re.compile(r'charset_normalizer'),          'charset-normalizer'),
    (re.compile(r'\bblack\b'),                   'black'),
    (re.compile(r'\bmypy[^c]|\bmypy$'),          'mypy'),
]


def _detect_mypyc_broken_packages(stderr_text: str) -> list[str]:
    """Detect which pip packages have broken mypyc extensions from the traceback.

    Returns a list of pip package names to force-reinstall.
    """
    packages = set()
    # Look for known package names in the traceback context
    for pattern, pip_name in _MYPYC_PACKAGE_PATTERNS:
        if pattern.search(stderr_text):
            packages.add(pip_name)
    # Fallback: if we see __mypyc but can't identify the package,
    # force-reinstall charset-normalizer (by far the most common culprit)
    if not packages and '__mypyc' in stderr_text:
        packages.add('charset-normalizer')
    return sorted(packages)


def _try_fix_mypyc(stderr_text: str) -> bool:
    """Try to fix broken mypyc compiled extensions by force-reinstalling.

    Returns True if packages were reinstalled (caller should retry server).
    """
    packages = _detect_mypyc_broken_packages(stderr_text)
    if not packages:
        return False

    pkg_str = ', '.join(packages)
    _bus.emit('phase', json.dumps({
        'id': 'mypyc-fix',
        'label': f'🔧 Fixing broken mypyc extensions: {pkg_str}',
        'status': 'active',
        'detail': 'These packages have compiled C extensions that don\'t match '
                  'your Python version. Force-reinstalling to get correct wheels…',
    }))
    _bus.emit('log', f'Detected broken mypyc extensions in: {pkg_str}')
    _bus.emit('log', 'Running pip install --force-reinstall to fix…')

    cmd = [sys.executable, '-m', 'pip', 'install', '--force-reinstall',
           '--no-input'] + packages
    _bus.emit('log', f'$ {" ".join(cmd)}')

    try:
        proc = subprocess.Popen(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, bufsize=1, cwd=BASE_DIR)
    except Exception as e:
        _bus.emit('log', f'Failed to run pip: {e}')
        _bus.emit('phase', json.dumps({
            'id': 'mypyc-fix',
            'label': '🔧 mypyc fix — pip failed to start',
            'status': 'error',
        }))
        return False

    for line in proc.stdout:
        line = line.rstrip('\n')
        _bus.emit('pip_output', line)

    proc.wait(timeout=PIP_TIMEOUT)

    if proc.returncode == 0:
        _bus.emit('log', f'✅ Force-reinstalled: {pkg_str}')
        _bus.emit('phase', json.dumps({
            'id': 'mypyc-fix',
            'label': f'🔧 Fixed mypyc extensions: {pkg_str}',
            'status': 'done',
        }))
        return True
    else:
        _bus.emit('log', f'❌ pip install --force-reinstall failed (exit code {proc.returncode})')
        _bus.emit('phase', json.dumps({
            'id': 'mypyc-fix',
            'label': '🔧 mypyc fix failed',
            'status': 'error',
            'detail': f'Exit code {proc.returncode}. Try manually: '
                      f'pip install --force-reinstall {pkg_str}',
        }))
        return False


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

    print(f'[bootstrap] 🚀 Starting Tofu (host={host}, port={port})…',
          file=sys.stderr)

    # ── First attempt (fast path — no status page) ──
    # A healthy server.py blocks forever (app.run).  If _try_start_server
    # returns at all, the process crashed.  On clean shutdown (rc=0, e.g.
    # Ctrl+C) it calls sys.exit(0) internally — so reaching here means crash.
    _, stderr_text, rc = _try_start_server(first_attempt=True)

    # ── SIGKILL (OOM killer / external reaper): record + auto-relaunch ──
    # The server ran fine and was killed by the environment — dependencies
    # are not the problem, so skip the LLM repair flow entirely. If the
    # relaunched server then crashes with a REAL error, the function hands
    # us its (stderr, rc) and we enter repair mode with the real data.
    if _is_external_kill(rc):
        stderr_text, rc = _restart_after_external_kill(rc)

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

    # ── Fast path: mypyc broken extensions (no LLM needed) ──
    # Packages like charset-normalizer ship mypyc-compiled .so files that
    # are platform/Python-version specific.  When they don't match, every
    # import that touches requests/urllib3 fails.  Fix: force-reinstall.
    if _is_mypyc_error(stderr_text) and _try_fix_mypyc(stderr_text):
        _bus.emit('phase', json.dumps({
            'id': 'mypyc-retry',
            'label': '🔄 Retrying server.py after mypyc fix…',
            'status': 'active',
        }))
        _bus.emit('log', 'Restarting server.py…')
        _bus.emit('phase', json.dumps({
            'id': 'handoff-mypyc',
            'label': '🔄 Handing off to server.py — this may take a moment…',
            'status': 'active',
            'detail': 'The server is starting up (database init, migrations, etc.).',
        }))
        time.sleep(0.5)

        _stop_status_server(status_server)
        status_server = None

        _, stderr_text, rc = _try_start_server()
        # If _try_start_server returns, the server crashed again.
        # Re-open status page and fall through to normal repair flow.
        _bus = EventBus()
        status_server = _start_status_server(host, port)
        _bus.emit('phase', json.dumps({
            'id': 'mypyc-retry',
            'label': '🔄 Still failing after mypyc fix',
            'status': 'error',
            'detail': f'Exit code {rc}',
        }))
        _bus.emit('error_text', stderr_text[-3000:])
        # Fall through to requirements.txt / LLM repair below

    # ── Fast path: requirements.txt (no LLM needed) ──
    # For import / package errors, try installing from requirements.txt
    # first.  This is essential for freshly-exported projects where the
    # LLM API hasn't been configured yet.
    if _is_import_or_package_error(stderr_text) and _try_requirements_txt():
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
            print('[bootstrap] ⚠ Still failing after requirements.txt install.',
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
        elif (_legacy_pg_runtime_requested()
              and _is_pg_missing_error(stderr_text)):
            # Explicit PostgreSQL backend selection: required binaries are missing.
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
                print('[bootstrap] ❌ Non-dependency error and no LLM API configured.',
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
        print('[bootstrap] ❌ No LLM API key configured. '
              'Set LLM_API_KEY in .env and retry.', file=sys.stderr)
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
