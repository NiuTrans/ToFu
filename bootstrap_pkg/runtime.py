"""Shared runtime state: .env/config, SSE event bus, LLM diagnosis call.

STDLIB-ONLY CONTRACT — see bootstrap_pkg.env_reexec.
Bus/restart flag live HERE as module attributes; consumers MUST access them
as ``runtime._bus`` / helper calls so main()'s rebinding stays visible
(the historical ``global _bus; _bus = EventBus()`` semantics).
"""
from __future__ import annotations

import json
from collections import deque
import os
import queue
import re
import textwrap
import threading
import urllib.error
import urllib.parse
import urllib.request

from tofu_dotenv import load_dotenv_file

from .env_reexec import BASE_DIR

def _load_dotenv() -> None:
    """Load .env file (same logic as server.py)."""
    load_dotenv_file(os.path.join(BASE_DIR, '.env'))


def _runtime_port() -> int:
    """Return one valid configured TCP port or fail with an actionable error."""
    raw_port = os.environ.get('PORT', '15000')
    try:
        port = int(raw_port)
    except (TypeError, ValueError) as exc:
        raise ValueError(
            f'PORT must be an integer from 1 to 65535 (got {raw_port!r})') from exc
    if not 1 <= port <= 65535:
        raise ValueError(
            f'PORT must be an integer from 1 to 65535 (got {raw_port!r})')
    return port


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
            'https://api.openai.com/v1') or 'https://api.openai.com/v1',
        'model': os.environ.get('LLM_MODEL', 'gpt-4.1-mini') or 'gpt-4.1-mini',
        'host': os.environ.get('BIND_HOST', '0.0.0.0'),
        'port': _runtime_port(),
    }
class EventBus:
    """Pub/sub for SSE events.  Multiple browser tabs can subscribe."""

    MAX_EVENTS = 200
    MAX_BYTES = 256 * 1024
    MAX_EVENT_BYTES = 16 * 1024
    MAX_SUBSCRIBERS = 8

    def __init__(self):
        self._subscribers: list[queue.Queue] = []
        self._lock = threading.Lock()
        self._history = deque()
        self._history_bytes = 0

    def subscribe(self) -> queue.Queue:
        q: queue.Queue = queue.Queue(maxsize=self.MAX_EVENTS)
        with self._lock:
            if len(self._subscribers) >= self.MAX_SUBSCRIBERS:
                raise RuntimeError('Too many status streams')
            for evt, _size in self._history:
                q.put_nowait(evt)
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
        event = event.replace('\r', '').replace('\n', '')[:128]
        payload = payload.encode('utf-8')[-self.MAX_EVENT_BYTES:].decode('utf-8', errors='ignore')
        evt = {'event': event, 'data': payload}
        size = len(event.encode('utf-8')) + len(payload.encode('utf-8'))
        with self._lock:
            self._history.append((evt, size))
            self._history_bytes += size
            while len(self._history) > self.MAX_EVENTS or self._history_bytes > self.MAX_BYTES:
                _, removed = self._history.popleft()
                self._history_bytes -= removed
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
        - Personal deployments use SQLite. Distributed PostgreSQL is an
          externally managed platform dependency. Never suggest installing a
          database server, changing deployment mode, or falling back between
          storage authorities.

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
                response_bytes = resp.read(64 * 1024 + 1)
                if len(response_bytes) > 64 * 1024:
                    raise ValueError('Diagnosis response exceeds 64 KiB')
                raw = json.loads(response_bytes.decode())
            content = raw['choices'][0]['message']['content']
            # Strip markdown fences if present
            content = re.sub(r'^```(?:json)?\s*', '', content.strip())
            content = re.sub(r'\s*```$', '', content.strip())
            diagnosis = json.loads(content)
            if (not isinstance(diagnosis, dict)
                    or not isinstance(diagnosis.get('packages', []), list)
                    or not isinstance(diagnosis.get('diagnosis', ''), str)
                    or not isinstance(diagnosis.get('unresolvable', False), bool)):
                raise ValueError('Invalid diagnosis response shape')
            return diagnosis
        except Exception as e:
            last_err = e
            continue

    return {
        'packages': [],
        'diagnosis': f'Could not reach LLM API to diagnose the error: {last_err}',
        'unresolvable': True,
    }


def request_restart() -> None:
    """Set the restart flag (POST /bootstrap/save-config → main loop)."""
    global _restart_requested
    _restart_requested = True


def consume_restart_request() -> bool:
    """Read-and-clear the restart flag; True exactly once per request."""
    global _restart_requested
    if _restart_requested:
        _restart_requested = False
        return True
    return False
