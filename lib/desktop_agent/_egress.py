"""Desktop Agent — egress executor (``egress_http``).

Executes an HTTP request from the USER's machine (which typically has the
working proxy/VPN) on behalf of the Tofu server, whose own egress may be
geo-blocked. Only subscription-provider hosts are reachable: the whitelist
is hardcoded HERE and re-checked on every command — the agent never trusts
a server-supplied "extra allow" field (defense in depth, design §7.1).

Proxy discovery order for ``proxy_mode='env'`` (the default):
  process env vars → OS system proxy (Clash "system proxy" writes the
  registry / macOS system settings, NOT env vars — without this step the
  agent would still go out naked on the user's machine and the whole
  scheme spins its wheels) → direct.

Log hygiene: request/response bodies and headers carry the user's
subscription access token — this module logs only method/host/status/ms.
"""

from __future__ import annotations

import base64
import json
import os
import subprocess
import threading
import time
from urllib.parse import urlparse

import requests

from lib.log import get_logger

logger = get_logger(__name__)

#: Hardcoded agent-side whitelist (mirror of lib/desktop/egress.py —
#: deliberately a SECOND copy: a compromised server cannot widen it).
_ALLOWED_HOSTS = frozenset({
    'api.anthropic.com',
    'console.anthropic.com',
    'platform.claude.com',
    'claude.ai',
    'auth.openai.com',
    'auth0.openai.com',
    'chatgpt.com',
    'api.openai.com',
})

#: Loopback target class (E4 subscription adapter): only THIS machine's
#: loopback, and only the port the agent's OWN adapter policy occupies —
#: a compromised server cannot aim the relay at an arbitrary local service
#: (design §4.4: whitelist is the agent's adapter port, not a server claim).
_LOOPBACK_HOSTS = frozenset({'127.0.0.1', 'localhost', '::1'})

_MAX_BODY_BYTES = 2 * 1024 * 1024       # request body cap (design §7.3)
_MAX_RESPONSE_BYTES = 10 * 1024 * 1024  # non-stream response cap

try:
    from lib.compat import IS_WINDOWS as _IS_WINDOWS, IS_MACOS as _IS_MACOS
except Exception:  # pragma: no cover - compat shim always present in-app
    import sys as _sys
    _IS_WINDOWS = _sys.platform.startswith('win')
    _IS_MACOS = _sys.platform == 'darwin'


def _host_allowed(url: str, target: str = 'subscription') -> bool:
    try:
        parsed = urlparse(url)
        host = (parsed.hostname or '').lower()
    except Exception as e:
        logger.debug('[Egress] url parse failed: %s', e)
        return False
    if target == 'loopback':
        if host not in _LOOPBACK_HOSTS:
            return False
        try:
            from lib.desktop_agent._adapter import adapter_loopback_port
            allowed_port = adapter_loopback_port()
        except Exception as e:
            logger.debug('[Egress] adapter port unreadable: %s', e)
            return False
        return bool(allowed_port) and (parsed.port or 0) == allowed_port
    return host in _ALLOWED_HOSTS


def _windows_proxy_url() -> str:
    """Read the Windows "system proxy" (Clash's 允许局域网/系统代理 writes
    here) from the registry. '' when disabled/unavailable."""
    try:
        import winreg  # type: ignore
        key = winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            r'Software\Microsoft\Windows\CurrentVersion\Internet Settings')
        enable, _ = winreg.QueryValueEx(key, 'ProxyEnable')
        server, _ = winreg.QueryValueEx(key, 'ProxyServer')
        if enable and server:
            server = str(server).strip()
            # ProxyServer can be "http=host:1;https=host:2" — prefer https.
            if '=' in server and '://' not in server:
                parts = dict(p.split('=', 1) for p in server.split(';') if '=' in p)
                server = parts.get('https') or parts.get('http') or ''
            if server:
                return server if '://' in server else f'http://{server}'
    except Exception as e:
        logger.debug('[Egress] winreg proxy read failed: %s', e)
    return ''


def _macos_proxy_url() -> str:
    """Parse ``scutil --proxy`` for the macOS system proxy (HTTPS preferred)."""
    try:
        out = subprocess.run(
            ['scutil', '--proxy'], capture_output=True, text=True, timeout=5)
        if out.returncode != 0:
            return ''
        kv = {}
        for line in out.stdout.splitlines():
            line = line.strip()
            if ' : ' in line:
                k, v = line.split(' : ', 1)
                kv[k.strip()] = v.strip()
        for proto in ('HTTPS', 'HTTP'):
            if kv.get(f'{proto}Enable') == '1' and kv.get(f'{proto}Proxy'):
                host = kv[f'{proto}Proxy']
                port = kv.get(f'{proto}Port', '8080')
                return f'http://{host}:{port}'
    except Exception as e:
        logger.debug('[Egress] scutil proxy read failed: %s', e)
    return ''


def _os_proxy_url() -> str:
    """OS-level system proxy URL, '' when none/disabled/unsupported."""
    if _IS_WINDOWS:
        return _windows_proxy_url()
    if _IS_MACOS:
        return _macos_proxy_url()
    return ''


def _resolve_proxies(mode: str):
    """Map proxy_mode → requests ``proxies=`` argument.

    ``direct`` → bypass everything (incl. env); ``env``/``auto`` → let
    requests honor env vars (None), falling back to the OS system proxy
    when env is silent. ``auto`` additionally retries direct on connection
    failure (handled by the caller).
    """
    if mode == 'direct':
        return {'no_proxy': '*'}
    env_has = any(os.environ.get(k) for k in
                  ('HTTPS_PROXY', 'https_proxy', 'HTTP_PROXY', 'http_proxy'))
    if env_has:
        return None  # requests honors env vars itself
    os_proxy = _os_proxy_url()
    if os_proxy:
        return {'http': os_proxy, 'https': os_proxy}
    return None


def _do_request(method, url, headers, body, timeout_s, proxies):
    t0 = time.monotonic()
    resp = requests.request(method, url, headers=headers, data=body,
                            timeout=timeout_s, proxies=proxies, stream=False)
    elapsed_ms = int((time.monotonic() - t0) * 1000)
    content = resp.content
    if len(content) > _MAX_RESPONSE_BYTES:
        return {'status': 0,
                'error': f'response too large ({len(content)} bytes > {_MAX_RESPONSE_BYTES})'}
    return {
        'status': resp.status_code,
        'headers': {k: v for k, v in resp.headers.items()
                    if k.lower() != 'set-cookie'},
        'body_b64': base64.b64encode(content).decode('ascii'),
        'elapsed_ms': elapsed_ms,
    }


# ══════════════════════════════════════════════════════════
#  Streamed variant (egress_http_stream) + in-flight cancel (S3)
# ══════════════════════════════════════════════════════════

#: In-flight streamed requests: stream_id → live ``requests.Response``.
#: ``egress_cancel`` closes the response, which makes the agent's own read
#: loop raise and unwind — the upstream stops generating (and billing).
_INFLIGHT: dict = {}
_INFLIGHT_LOCK = threading.Lock()
#: Cancelled-or-finished stream ids — frames arriving after this point are
#: dropped (a cancel can race the tail of a finishing stream).
_SETTLED: set = set()

_FRAME_BYTES = 64 * 1024  # 64 KB per body frame (design §4.2)


def cancel_inflight(stream_id: str) -> bool:
    """Abort a live streamed request. Idempotent + silent on unknown ids
    (the stream may already have finished)."""
    if not stream_id:
        return False
    with _INFLIGHT_LOCK:
        resp = _INFLIGHT.pop(stream_id, None)
        _SETTLED.add(stream_id)
    if resp is None:
        logger.info('[Egress] cancel for unknown/finished stream %s',
                    stream_id[:8])
        return False
    try:
        resp.close()
        logger.info('[Egress] cancelled in-flight stream %s', stream_id[:8])
    except Exception as e:
        logger.debug('[Egress] cancel close failed for %s: %s', stream_id[:8], e)
    return True


def _emit(on_chunk, stream_id, seq, stream, data, done=False):
    """Push one frame unless the stream is already settled (cancelled)."""
    with _INFLIGHT_LOCK:
        if stream_id in _SETTLED and not done:
            return False
    on_chunk(seq, stream, data)
    return True


def start_egress_stream(params: dict, on_chunk, on_exit):
    """Stream an HTTP response back through frames (off the poll loop).

    ``on_chunk(seq, stream, data)`` per frame: ``meta`` first (status +
    headers JSON), then ``body`` (base64 chunks ≤64 KB), ``error`` on a
    mid-stream network failure. ``on_exit(outcome)`` fires exactly once
    with the final stats (or a refusal ``{'error': …}``).

    Runs on the CALLER's thread — the poll loop hands it a daemon thread
    (``_start_egress_stream_streamed``), exactly like project_run_command.
    """
    url = str((params or {}).get('url') or '')
    stream_id = str(params.get('stream_id') or '')
    target = str((params or {}).get('target') or 'subscription')
    seq = [0]

    def _seq():
        seq[0] += 1
        return seq[0]

    def _refuse(msg):
        logger.warning('[Egress] stream refused: %s', msg[:120])
        on_exit({'error': msg})

    if not _host_allowed(url, target):
        return _refuse('egress host not in agent whitelist')
    if not stream_id:
        return _refuse('missing stream_id')
    method = str(params.get('method') or 'POST').upper()
    if method not in ('GET', 'POST'):
        return _refuse(f'unsupported method: {method}')
    try:
        body = base64.b64decode(params.get('body_b64') or '')
    except Exception as e:
        logger.debug('[Egress] bad body_b64: %s', e)
        return _refuse(f'bad body_b64: {e}')
    if len(body) > _MAX_BODY_BYTES:
        return _refuse(f'request body too large ({len(body)} bytes)')
    headers = params.get('headers') or {}
    timeout_s = min(max(int(params.get('timeout_ms') or 600000) / 1000.0, 30), 600)
    mode = str(params.get('proxy_mode') or 'env')
    host = urlparse(url).hostname or '?'
    t0 = time.monotonic()
    total_bytes = 0
    status = 0

    try:
        resp = requests.request(
            method, url, headers=headers, data=body,
            timeout=(min(timeout_s, 60), timeout_s), proxies=_resolve_proxies(mode),
            stream=True)
    except Exception as e:
        logger.warning('[Egress] stream connect %s failed: %s', host, e)
        on_chunk(_seq(), 'error',
                 json.dumps({'message': f'{type(e).__name__}: {e}',
                             'where': 'connect'}))
        on_exit({'error': str(e), 'status': 0})
        return

    status = resp.status_code
    with _INFLIGHT_LOCK:
        _INFLIGHT[stream_id] = resp
    try:
        meta = json.dumps({
            'status': status,
            'headers': {k: v for k, v in resp.headers.items()
                        if k.lower() != 'set-cookie'},
        })
        if not _emit(on_chunk, stream_id, _seq(), 'meta', meta):
            return
        logger.info('[Egress] stream %s %s → %s (id=%s)',
                    method, host, status, stream_id[:8])
        for chunk in resp.iter_content(_FRAME_BYTES):
            if not chunk:
                continue
            with _INFLIGHT_LOCK:
                if stream_id in _SETTLED:
                    logger.info('[Egress] stream %s dropped (cancelled)',
                                stream_id[:8])
                    on_exit({'status': status, 'bytes': total_bytes,
                             'cancelled': True})
                    return
            total_bytes += len(chunk)
            if not _emit(on_chunk, stream_id, _seq(), 'body',
                         base64.b64encode(chunk).decode('ascii')):
                on_exit({'status': status, 'bytes': total_bytes,
                         'cancelled': True})
                return
    except Exception as e:
        with _INFLIGHT_LOCK:
            cancelled = stream_id in _SETTLED
        if cancelled:
            on_exit({'status': status, 'bytes': total_bytes,
                     'cancelled': True})
            return
        logger.warning('[Egress] stream %s mid-read failed: %s',
                       stream_id[:8], e)
        _emit(on_chunk, stream_id, _seq(), 'error',
              json.dumps({'message': f'{type(e).__name__}: {e}',
                          'where': 'read'}))
        on_exit({'error': str(e), 'status': status})
        return
    finally:
        with _INFLIGHT_LOCK:
            _INFLIGHT.pop(stream_id, None)
            _SETTLED.add(stream_id)
        try:
            resp.close()
        except Exception as e:
            logger.debug('[Egress] stream close failed: %s', e)

    logger.info('[Egress] stream %s done: %s bytes in %.1fs',
                stream_id[:8], total_bytes, time.monotonic() - t0)
    on_exit({'status': status,
             'bytes': total_bytes,
             'elapsed_ms': int((time.monotonic() - t0) * 1000)})


def cmd_egress_http(params: dict) -> dict:
    """Execute one HTTP request on behalf of the server (``egress_http``).

    Params: ``url`` (whitelisted), ``method``, ``headers``, ``body_b64``,
    ``timeout_ms``, ``proxy_mode`` (env/direct/auto). Returns the wire
    result dict; network failures come back as ``status: 0`` + ``error``.
    """
    url = str((params or {}).get('url') or '')
    target = str((params or {}).get('target') or 'subscription')
    if not _host_allowed(url, target):
        logger.warning('[Egress] REFUSED non-whitelisted host: %s (target=%s)',
                       (urlparse(url).hostname or url)[:80], target)
        return {'error': 'egress host not in agent whitelist'}
    method = str(params.get('method') or 'POST').upper()
    if method not in ('GET', 'POST'):
        return {'error': f'unsupported method: {method}'}
    try:
        body = base64.b64decode(params.get('body_b64') or '')
    except Exception as e:
        logger.debug('[Egress] bad body_b64: %s', e)
        return {'error': f'bad body_b64: {e}'}
    if len(body) > _MAX_BODY_BYTES:
        return {'error': f'request body too large ({len(body)} bytes)'}
    headers = params.get('headers') or {}
    if not isinstance(headers, dict):
        return {'error': 'headers must be a dict'}
    timeout_s = min(max(int(params.get('timeout_ms') or 30000) / 1000.0, 1), 60)
    mode = str(params.get('proxy_mode') or 'env')

    host = urlparse(url).hostname or '?'
    try:
        out = _do_request(method, url, headers, body, timeout_s,
                          _resolve_proxies(mode))
        logger.info('[Egress] %s %s → %s (%sms)',
                    method, host, out.get('status'), out.get('elapsed_ms', '?'))
        return out
    except (requests.ConnectionError, requests.Timeout) as e:
        if mode == 'auto':
            logger.info('[Egress] %s via proxy failed (%s) — retrying direct',
                        host, e)
            try:
                out = _do_request(method, url, headers, body, timeout_s,
                                  {'no_proxy': '*'})
                logger.info('[Egress] %s %s direct → %s', method, host,
                            out.get('status'))
                return out
            except Exception as e2:
                logger.warning('[Egress] %s direct retry failed: %s', host, e2)
                return {'status': 0, 'error': f'{type(e2).__name__}: {e2}'}
        logger.warning('[Egress] %s %s network error: %s', method, host, e)
        return {'status': 0, 'error': f'{type(e).__name__}: {e}'}
    except Exception as e:
        logger.error('[Egress] %s %s failed: %s', method, host, e, exc_info=True)
        return {'status': 0, 'error': f'{type(e).__name__}: {e}'}
