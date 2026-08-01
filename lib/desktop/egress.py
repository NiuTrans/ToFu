"""lib/desktop/egress.py — 订阅流量的桌面代理出口路由层（S2）。

Routes OAuth token exchange/refresh (and, from S3, LLM streams) through
the user's desktop agent when the SERVER's own egress is geo-blocked or
network-dead — the failure chain measured 2026-07-31 (direct DNS dead,
corporate proxy 403 ``Request not allowed`` on all five endpoints).

Decision flow (:func:`route_request`):

  1. probe the target host WITHOUT auth (per-host 300s cache) —
     ``ok`` (401/400/404/405/200 = app layer reached) → caller goes direct;
  2. ``geo_blocked`` (403) or ``network_fail`` → pick an online
     egress-capable agent in the caller's tenant (legacy ``''`` user = any
     agent); none → :class:`EgressUnavailable` with actionable guidance.

Everything here is transport-agnostic: callers get an
:class:`EgressResponse` shaped like ``requests.Response`` so the OAuth
modules barely notice the detour. The domain whitelist is enforced BOTH
here (before enqueueing) and in the agent executor (before opening the
socket) — defense in depth, exact host match only.

Design: docs/DESKTOP_EGRESS_DESIGN.md §6.1.
"""

from __future__ import annotations

import base64
import json
import time
import uuid
from urllib.parse import urlparse

from lib.json_store import read_json
from lib.log import get_logger
from lib.ttl_cache import TTLCache

logger = get_logger(__name__)

__all__ = [
    'ALLOWED_EGRESS_HOSTS',
    'EgressUnavailable',
    'EgressResponse',
    'host_allowed',
    'route_request',
    'egress_http',
]

#: Exact-match egress whitelist (§7.1). NO suffix matching —
#: ``api.anthropic.com.evil.com`` must never pass.
ALLOWED_EGRESS_HOSTS = frozenset({
    'api.anthropic.com',
    'console.anthropic.com',
    'claude.ai',
    'auth.openai.com',
    'auth0.openai.com',
    'chatgpt.com',
    'api.openai.com',
})

#: Per-host direct-reachability verdicts ('ok' / 'geo_blocked' /
#: 'network_fail'), 300s TTL — probing on every exchange would add seconds.
_probe_cache = TTLCache(ttl=300, max_size=32)

#: egress_http bridge-command TTL (design §4.3: token calls are short).
_EGRESS_HTTP_TTL_S = 120


class EgressUnavailable(RuntimeError):
    """No usable path to the provider: direct is blocked AND no suitable
    desktop agent is online. The message always carries user-actionable
    guidance (start an agent / pick one in Settings)."""


class EgressResponse:
    """``requests.Response``-shaped result for the egress path."""

    def __init__(self, status: int, headers: dict, content: bytes,
                 elapsed_ms: int = 0):
        self.status_code = status
        self.headers = headers or {}
        self.content = content or b''
        self.elapsed_ms = elapsed_ms

    @property
    def text(self) -> str:
        return self.content.decode('utf-8', errors='replace')

    def json(self):
        return json.loads(self.text)


def host_allowed(url: str) -> bool:
    """Exact-host whitelist check (server side — the agent re-checks)."""
    try:
        host = (urlparse(url).hostname or '').lower()
    except Exception as e:
        logger.debug('[Egress] url parse failed for %r: %s', url, e)
        return False
    return host in ALLOWED_EGRESS_HOSTS


def _probe_host(url: str) -> str:
    """Classify direct reachability of the URL's host.

    POST the real endpoint WITHOUT auth: a 401/400/404/405 proves the
    application layer answered (network path OK — 'ok'); 403 is the
    geo-block signature; an exception is a network failure. Cached per host.
    """
    host = (urlparse(url).hostname or '').lower()
    cached = _probe_cache.get(host)
    if cached:
        return cached
    verdict = 'network_fail'
    try:
        from lib.http_client import http_post
        resp = http_post(url, json={}, timeout=5)
        verdict = 'geo_blocked' if resp.status_code == 403 else 'ok'
    except Exception as e:
        logger.debug('[Egress] direct probe of %s failed: %s', host, e)
    _probe_cache.set(host, verdict)
    logger.info('[Egress] probe %s → %s', host, verdict)
    return verdict


def _online_egress_agents(user_id: str = '') -> list:
    """Online agents advertising the egress capability, tenant-scoped.

    Legacy single-user deployments (``user_id=''``) see every egress agent
    (bridge v1/legacy semantics); a real tenant only sees its own.
    """
    from lib.desktop import online_agents
    out = []
    for a in online_agents():
        caps = a.get('capabilities') or {}
        if not caps.get('egress'):
            continue
        if user_id and (a.get('user_id') or '') != user_id:
            continue
        out.append(a)
    return out


def _pinned_agent(user_id: str) -> str:
    """The user's pinned egress agent id from
    ``data/config/oauth_egress_agents.json`` (Settings → OAuth, multi-agent
    deployments). '' when unset."""
    try:
        from lib.config_dir import config_path
        data = read_json(config_path('oauth_egress_agents.json'), default={}) or {}
        return str(data.get(user_id or '', '') or '')
    except Exception as e:
        logger.debug('[Egress] pinned-agent read failed: %s', e)
        return ''


def route_request(url: str, *, user_id: str = '') -> str:
    """Decide how a request to *url* should leave this server.

    Returns ``'direct'`` when the server's own egress reaches the provider,
    else the agent_id to route through.

    Raises:
        EgressUnavailable: direct is blocked AND no suitable agent is
            online (or several are and none is pinned).
    """
    # Only subscription-provider hosts are egress-eligible at all. Probing
    # (let alone rerouting) arbitrary gateways would waste a request per
    # host and could bend internal traffic to an agent that whitelists only
    # these seven domains — everything else is direct, always, unprobed.
    if not host_allowed(url):
        return 'direct'
    if _probe_host(url) == 'ok':
        return 'direct'
    agents = _online_egress_agents(user_id)
    if not agents:
        raise EgressUnavailable(
            'server egress to this provider is blocked and no egress-capable '
            'desktop agent is online — start the desktop agent with '
            '--allow-egress on a machine whose network can reach the provider')
    if len(agents) == 1:
        return agents[0]['agent_id']
    pinned = _pinned_agent(user_id)
    if pinned and any(a['agent_id'] == pinned for a in agents):
        return pinned
    raise EgressUnavailable(
        f'{len(agents)} egress-capable agents are online; pin one in Settings '
        '(oauth_egress_agent_id) instead of letting the server guess')


# ══════════════════════════════════════════════════════════
#  Streaming (S3): open_stream / EgressStreamReader / cancel_stream
# ══════════════════════════════════════════════════════════

#: egress_http_stream bridge-command TTL (design §4.3: 30-min LLM streams).
_EGRESS_STREAM_TTL_S = 1800
#: How long open_stream waits for the agent's meta (headers) frame.
_META_TIMEOUT_S = 60
#: Consumer poll cadence for new frames.
_CONSUME_POLL_S = 0.2
#: Half-open watchdog: no new frame for this long AND the agent has dropped
#: out of the online registry → declare the stream dead (design §4.3).
_STALL_S = 30


class EgressStreamReader:
    """``requests.Response``-shaped reader over a desktop-egress stream.

    Consumes ``{seq, stream, data}`` frames from the bridge stream store:
    ``meta`` (status/headers), ``body`` (base64 chunks), ``error``
    (mid-stream failure). Presents the subset of the Response surface the
    sync LLM transport uses: ``.status_code`` / ``.headers`` /
    ``.iter_lines(decode_unicode=True)`` / ``.close()`` (+ arbitrary
    attribute assignment like ``.encoding``).
    """

    def __init__(self, cmd_id: str, agent_id: str, user_id: str = ''):
        self._cmd_id = cmd_id
        self._agent_id = agent_id
        self._user_id = user_id
        self._seq = 0
        self._buf = b''
        self._drained: list = []
        self._done = False
        self._closed = False
        self._last_frame_at = time.monotonic()
        self.status_code = 0
        self.headers: dict = {}

    # ── frame intake ────────────────────────────────────────────

    def _poll_frames(self):
        """Pull new frames into the buffer. Raises EgressUnavailable on a
        vanished entry (swept / agent died) or an agent-side error frame."""
        from lib.desktop import get_frames
        got = get_frames(self._cmd_id, since_seq=self._seq)
        if got is None:
            if self._done:
                return
            raise EgressUnavailable(
                f'egress stream {self._cmd_id[:8]} vanished mid-flight '
                '(swept or agent died)')
        frames, done = got
        for seq, stream, data in frames:
            self._seq = max(self._seq, seq)
            self._last_frame_at = time.monotonic()
            if stream == 'meta' and data:
                try:
                    meta = json.loads(data)
                except (json.JSONDecodeError, TypeError) as e:
                    logger.debug('[Egress] bad meta frame: %s', e)
                    continue
                self.status_code = int(meta.get('status') or 0)
                self.headers = meta.get('headers') or {}
            elif stream == 'body':
                try:
                    chunk = base64.b64decode(data)
                except Exception as e:
                    logger.debug('[Egress] bad body frame: %s', e)
                    continue
                self._buf += chunk
                self._drained.append(chunk)
            elif stream == 'error':
                raise EgressUnavailable(f'agent stream error: {data}')
        if done:
            self._done = True

    def _agent_online(self) -> bool:
        from lib.desktop import online_agents
        return any(a.get('agent_id') == self._agent_id
                   for a in online_agents())

    def _check_watchdog(self):
        if self._done:
            return
        idle = time.monotonic() - self._last_frame_at
        if idle >= _STALL_S and not self._agent_online():
            raise EgressUnavailable(
                f'egress stream {self._cmd_id[:8]} stalled {idle:.0f}s and '
                f'agent {self._agent_id[:8]} is offline — declaring dead')

    # ── Response surface ────────────────────────────────────────

    def wait_headers(self, timeout: float = _META_TIMEOUT_S):
        """Block until the agent's meta frame lands (status/headers set)."""
        deadline = time.monotonic() + timeout
        while self.status_code == 0:
            self._poll_frames()
            if self.status_code:
                return
            if time.monotonic() > deadline:
                raise EgressUnavailable(
                    f'agent produced no response headers within {timeout:.0f}s')
            time.sleep(_CONSUME_POLL_S)

    def _next_line(self):
        """One decoded line from the buffer, or None when no full line yet."""
        idx = self._buf.find(b'\n')
        if idx < 0:
            return None
        raw = self._buf[:idx]
        self._buf = self._buf[idx + 1:]
        if raw.endswith(b'\r'):
            raw = raw[:-1]
        return raw.decode('utf-8', errors='replace')

    def iter_lines(self, decode_unicode=True):
        """Yield decoded lines as they arrive (requests.iter_lines shape)."""
        while True:
            self._poll_frames()
            line = self._next_line()
            if line is not None:
                yield line
                continue
            if self._done:
                # Drain a trailing partial line (no final newline).
                if self._buf:
                    tail, self._buf = self._buf, b''
                    yield tail.decode('utf-8', errors='replace')
                return
            self._check_watchdog()
            time.sleep(_CONSUME_POLL_S)

    def read_all_text(self) -> str:
        """Drain the stream to done and return the whole body (error path)."""
        while not self._done:
            self._poll_frames()
            if not self._done:
                self._check_watchdog()
                time.sleep(_CONSUME_POLL_S)
        return b''.join(self._drained).decode('utf-8', errors='replace')

    def close(self):
        """Best-effort cancel: tell the agent to abort the upstream request
        (idempotent — the transport calls this from its finally block)."""
        if self._closed:
            return
        self._closed = True
        self._done = True
        try:
            cancel_stream(self._cmd_id, self._agent_id, self._user_id)
        except Exception as e:
            logger.debug('[Egress] cancel on close failed for %s: %s',
                         self._cmd_id[:8], e)


def open_stream(url: str, *, method: str = 'POST', headers: dict = None,
                body: bytes = b'', agent_id: str = None, user_id: str = '',
                log_prefix: str = '') -> EgressStreamReader:
    """Open a streamed request through the caller's desktop agent.

    Enqueues ``egress_http_stream`` (TTL 1800s) and waits for the agent's
    meta frame. Returns the :class:`EgressStreamReader` once response
    headers are known (mirroring connect-phase semantics).

    Raises:
        EgressUnavailable: host not whitelisted / no agent / no meta frame
            within the connect window.
    """
    if not host_allowed(url):
        raise EgressUnavailable(
            f'egress host not in whitelist: {urlparse(url).hostname or url!r}')
    if agent_id is None:
        agent_id = route_request(url, user_id=user_id)
    cmd_id = uuid.uuid4().hex
    params = {
        'url': url,
        'method': (method or 'POST').upper(),
        'headers': dict(headers or {}),
        'body_b64': base64.b64encode(body or b'').decode('ascii'),
        'timeout_ms': _EGRESS_STREAM_TTL_S * 1000,
        'proxy_mode': 'env',
        'stream_id': cmd_id,
    }
    from lib.desktop import enqueue_desktop_command
    enq_id, err = enqueue_desktop_command(
        'egress_http_stream', params,
        target_agent_id=agent_id, user_id=user_id,
        cmd_id=cmd_id, ttl=_EGRESS_STREAM_TTL_S)
    if err:
        raise EgressUnavailable(f'failed to enqueue egress stream: {err}')
    logger.info('%s[Egress] stream %s %s via agent %s (cmd=%s)',
                log_prefix, params['method'], urlparse(url).hostname,
                agent_id[:8], cmd_id[:8])
    reader = EgressStreamReader(cmd_id, agent_id, user_id)
    reader.wait_headers()
    return reader


def cancel_stream(cmd_id: str, agent_id: str, user_id: str = ''):
    """Fire-and-forget cancel for an in-flight stream (design §4.2)."""
    from lib.desktop import enqueue_desktop_command
    enq_id, err = enqueue_desktop_command(
        'egress_cancel', {'cmd_id': cmd_id},
        target_agent_id=agent_id, user_id=user_id)
    if err:
        logger.debug('[Egress] cancel enqueue failed for %s: %s', cmd_id[:8], err)


# ══════════════════════════════════════════════════════════
#  Status surface (S4): page-load-safe egress state per host
# ══════════════════════════════════════════════════════════

import threading

_probe_bg_lock = threading.Lock()
_probe_bg_fired: set = set()


def _spawn_background_probe(host: str) -> None:
    """Fire-and-forget a probe so the NEXT status poll has a cached verdict.

    The status surface must never probe inline (a direct-connect timeout is
    up to 5s of white screen on the settings page), so the first sight of a
    host just warms the cache on a daemon thread.
    """
    with _probe_bg_lock:
        if host in _probe_bg_fired:
            return
        _probe_bg_fired.add(host)

    def _run():
        try:
            url = f'https://{host}/'
            # Reuse the real probe (it writes the cache).
            _probe_host(url)
        except Exception as e:
            logger.debug('[Egress] background probe of %s failed: %s', host, e)
        finally:
            with _probe_bg_lock:
                _probe_bg_fired.discard(host)

    threading.Thread(target=_run, daemon=True,
                     name=f'egress-probe-{host[:20]}').start()


def egress_status(host: str, *, user_id: str = '') -> dict:
    """Non-blocking egress state for a provider host (design §6.2 A4).

    States:
      ``direct``              — cached probe says the server reaches the host
      ``agent``               — blocked, and an egress-capable agent is online
      ``agent_no_capability`` — agent(s) online but NONE with --allow-egress
      ``unavailable``         — blocked and no suitable agent online
      ``unknown``             — no cached verdict (a background probe is
                                fired so the next poll knows)

    NEVER probes inline — reads the 300s probe cache only.
    """
    verdict = _probe_cache.get(host) or ''
    if not verdict:
        _spawn_background_probe(host)
        return {'state': 'unknown', 'verdict': '', 'agents': []}
    if verdict == 'ok':
        return {'state': 'direct', 'verdict': verdict, 'agents': []}
    from lib.desktop import online_agents
    all_agents = online_agents()
    capable = [a for a in all_agents
               if (a.get('capabilities') or {}).get('egress')
               and (not user_id or (a.get('user_id') or '') == user_id)]
    if capable:
        return {'state': 'agent', 'verdict': verdict,
                'agents': [{'agent_id': a['agent_id'], 'name': a.get('name', '')}
                           for a in capable]}
    scoped_any = [a for a in all_agents
                  if not user_id or (a.get('user_id') or '') == user_id]
    if scoped_any:
        return {'state': 'agent_no_capability', 'verdict': verdict,
                'agents': [{'agent_id': a['agent_id'], 'name': a.get('name', '')}
                           for a in scoped_any]}
    return {'state': 'unavailable', 'verdict': verdict, 'agents': []}


def egress_http(url: str, *, method: str = 'POST', headers: dict = None,
                body: bytes = b'', timeout: float = 30,
                user_id: str = '') -> EgressResponse:
    """Execute one HTTP request through the caller's desktop agent.

    Whitelist → agent selection → bridge command (TTL 120s) → adapt the
    agent result into :class:`EgressResponse`.

    Raises:
        EgressUnavailable: host not whitelisted, no agent, agent offline /
            timed out, or the agent hit a network error.
    """
    if not host_allowed(url):
        raise EgressUnavailable(
            f'egress host not in whitelist: {urlparse(url).hostname or url!r}')
    from lib.desktop import send_desktop_command
    agent_id = route_request(url, user_id=user_id)  # raises when unavailable
    params = {
        'url': url,
        'method': (method or 'POST').upper(),
        'headers': dict(headers or {}),
        'body_b64': base64.b64encode(body or b'').decode('ascii'),
        'timeout_ms': int(min(max(timeout, 1), 60) * 1000),
        'proxy_mode': 'env',
    }
    logger.info('[Egress] routing %s %s via agent %s (user=%s)',
                params['method'], urlparse(url).hostname, agent_id[:8],
                user_id or '(legacy)')
    result, error = send_desktop_command(
        'egress_http', params,
        timeout=min(timeout + 15, _EGRESS_HTTP_TTL_S),
        target_agent_id=agent_id, user_id=user_id,
        ttl=_EGRESS_HTTP_TTL_S)
    if error or result is None:
        raise EgressUnavailable(
            f'desktop agent failed to execute egress request: {error or "no result"}')
    status = int(result.get('status') or 0)
    if status == 0:
        raise EgressUnavailable(
            f'agent network error: {result.get("error") or "unknown"}')
    try:
        content = base64.b64decode(result.get('body_b64') or '')
    except Exception as e:
        raise EgressUnavailable(f'agent returned undecodable body: {e}') from e
    return EgressResponse(
        status=status,
        headers=result.get('headers') or {},
        content=content,
        elapsed_ms=int(result.get('elapsed_ms') or 0),
    )
