"""lib/desktop/egress.py — 订阅流量的桌面代理出口路由层（S2）。

Routes OAuth token exchange/refresh (and, from S3, LLM streams) through
the user's desktop agent when the SERVER's own egress is geo-blocked or
network-dead — the failure chain measured 2026-07-31 (direct DNS dead,
corporate proxy 403 ``Request not allowed`` on all five endpoints).

Decision flow (:func:`route_request`):

  1. race the target host WITHOUT auth over direct + every configured server
     proxy; the first application-layer success wins and the other routes
     finish probing in the background — ``ok`` means a server route exists;
  2. ``geo_blocked`` (403) or ``network_fail`` → pick an online
     egress-capable agent in the caller's tenant (legacy ``''`` user = any
     agent); none → :class:`EgressUnavailable` with actionable guidance.

Everything here is transport-agnostic: callers get an
:class:`EgressResponse` shaped like ``requests.Response`` so the OAuth
modules barely notice the detour. The domain whitelist is enforced BOTH
here (before enqueueing) and in the agent executor (before opening the
socket) — defense in depth, exact host match only.

Architecture: docs/modules/remote_execution.md.
"""

from __future__ import annotations

import base64
import json
import threading
import time
import uuid
from urllib.parse import urlparse

from lib.json_store import read_json
from lib.log import get_logger
from lib.proxy import SUBSCRIPTION_HOSTS
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
#: ``api.anthropic.com.evil.com`` must never pass. Aliases
#: ``lib.proxy.SUBSCRIPTION_HOSTS`` (single source of truth — the proxy
#: pool's ``subscription`` scope serves exactly this set).
ALLOWED_EGRESS_HOSTS = SUBSCRIPTION_HOSTS

#: Stable per-host verdicts. ``ok`` and ``geo_blocked`` may safely remain for
#: five minutes; a transient proxy/network failure may not. Keeping
#: ``network_fail`` here caused the 2026-08-10 incident where a 60s proxy
#: cooldown recovered but routing rejected Codex for the full 300s cache TTL.
_probe_cache = TTLCache(ttl=300, max_size=32)
_probe_network_fail_cache = TTLCache(ttl=30, max_size=32)


def _cached_probe_verdict(host: str) -> str:
    return (_probe_cache.get(host)
            or _probe_network_fail_cache.get(host)
            or '')


def invalidate_probe_cache() -> int:
    """Drop every cached reachability verdict. Called by lib.proxy whenever
    the proxy topology (pool / legacy address / bypass list) changes — a
    verdict measured through the OLD path must never route traffic under
    the NEW one (2026-08-07: pre-fix, a proxy change left stale
    ``geo_blocked`` verdicts misrouting subscription traffic to desktop
    agents for up to 300s)."""
    n = _probe_cache.clear() + _probe_network_fail_cache.clear()
    if n:
        logger.info('[Egress] probe cache invalidated (%d hosts) — proxy '
                    'topology changed', n)
    return n

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


#: Loopback target class (E4 subscription adapter): the relay may aim at
#: the AGENT's own loopback only, on the declared adapter port — never at
#: arbitrary local services. The agent re-checks against ITS OWN policy
#: port (lib/desktop_agent/_egress.py), so a spoofed loopback_port here
#: just fails closed on the far side.
_LOOPBACK_HOSTS = frozenset({'127.0.0.1', 'localhost', '::1'})


def _loopback_allowed(url: str, port: int) -> bool:
    try:
        parsed = urlparse(url)
        host = (parsed.hostname or '').lower()
    except Exception as e:
        logger.debug('[Egress] loopback url parse failed for %r: %s', url, e)
        return False
    return (host in _LOOPBACK_HOSTS and int(port or 0) > 0
            and (parsed.port or 0) == int(port))


def _check_target(url: str, target: str, loopback_port: int) -> None:
    """Raise EgressUnavailable when *url* fails its target-class whitelist."""
    if target == 'loopback':
        if not _loopback_allowed(url, loopback_port):
            raise EgressUnavailable(
                f'loopback relay target not allowed: {url!r} '
                f'(declared adapter port {loopback_port})')
    elif not host_allowed(url):
        raise EgressUnavailable(
            f'egress host not in whitelist: {urlparse(url).hostname or url!r}')


def _probe_host(url: str) -> str:
    """Classify server reachability through the subscription route manager.

    Route health is owned per ``(host, concrete path)`` by
    :mod:`lib.subscription_routes`; these small TTL caches are only a mirror
    for the non-blocking Settings status surface and never gate requests.
    """
    host = (urlparse(url).hostname or '').lower()
    verdict = _probe_host_paths(url, host)
    if verdict == 'network_fail':
        _probe_cache.invalidate(host)
        _probe_network_fail_cache.set(host, verdict)
    else:
        _probe_network_fail_cache.invalidate(host)
        _probe_cache.set(host, verdict)
    logger.info('[Egress] probe %s → %s', host, verdict)
    return verdict


def _probe_host_paths(url: str, host: str) -> str:
    """Run (or reuse) the concurrent direct + all-proxy route race."""
    from lib.proxy import (
        subscription_route_candidates,
        subscription_route_verdict,
    )
    routes = subscription_route_candidates(url)
    if routes:
        logger.info('[Egress] probe %s ok via %s', host, routes[0].label)
        return 'ok'
    verdict = subscription_route_verdict(url)
    return verdict if verdict in ('geo_blocked', 'network_fail') \
        else 'network_fail'


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


#: agent_id → monotonic ts of the last TRANSPORT-level egress success
#: (any delivered HTTP answer counts — a 429 still proves the path works).
#: Drives the multi-agent fallback order (G1, SUBSCRIPTION_RELAY_SCENARIOS
#: §4.2): pinned first, then most-recently-successful, then last_seen.
_last_success: dict = {}
_success_lock = threading.Lock()


def _note_success(agent_id: str) -> None:
    with _success_lock:
        _last_success[agent_id] = time.monotonic()


def _order_agents(agents: list, user_id: str) -> list:
    """pinned → recent egress success → most-recently-seen."""
    pinned = _pinned_agent(user_id)
    with _success_lock:
        snap = dict(_last_success)
    return sorted(
        agents,
        key=lambda a: (0 if a['agent_id'] == pinned else 1,
                       -snap.get(a['agent_id'], 0.0),
                       -(a.get('last_seen') or 0.0)))


def route_candidates(url: str, *, user_id: str = '') -> list:
    """Ordered egress candidate list for *url*.

    ``['direct']`` when the server's own egress reaches the provider;
    otherwise the online egress-capable agents in fallback order. Raises
    only when direct is blocked AND no suitable agent is online — a
    multi-agent deployment no longer REQUIRES a pin (the pin just leads
    the chain).
    """
    # Only subscription-provider hosts are egress-eligible at all. Probing
    # (let alone rerouting) arbitrary gateways would waste a request per
    # host and could bend internal traffic to an agent that whitelists only
    # these domains — everything else is direct, always, unprobed.
    if not host_allowed(url):
        return ['direct']
    verdict = _probe_host(url)
    if verdict == 'ok':
        return ['direct']
    agents = _online_egress_agents(user_id)
    if not agents:
        if verdict == 'network_fail':
            raise EgressUnavailable(
                'subscription egress is temporarily unreachable through the '
                'configured server proxy paths and no egress-capable desktop '
                'agent is online — every route will be re-probed after its '
                'short circuit cooldown; for an independent fallback, start the desktop '
                'agent with --allow-egress')
        raise EgressUnavailable(
            'server egress to this provider is blocked and no egress-capable '
            'desktop agent is online — start the desktop agent with '
            '--allow-egress on a machine whose network can reach the provider')
    return [a['agent_id'] for a in _order_agents(agents, user_id)]


def route_request(url: str, *, user_id: str = '') -> str:
    """First (best) egress route for *url* — see :func:`route_candidates`."""
    return route_candidates(url, user_id=user_id)[0]


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
    ``.iter_content(chunk_size=...)`` / ``.close()`` (+ the historical
    ``iter_lines`` compatibility surface and arbitrary
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

    def iter_content(self, chunk_size=64 << 10):
        """Yield raw upstream bytes without performing SSE line parsing."""
        try:
            limit = max(1, int(chunk_size or (64 << 10)))
        except (TypeError, ValueError, OverflowError):
            limit = 64 << 10
        while True:
            self._poll_frames()
            while self._buf:
                chunk = self._buf[:limit]
                self._buf = self._buf[len(chunk):]
                yield chunk
            if self._done:
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
                log_prefix: str = '', target: str = 'subscription',
                loopback_port: int = 0) -> EgressStreamReader:
    """Open a streamed request through the caller's desktop agent.

    Enqueues ``egress_http_stream`` (TTL 1800s) and waits for the agent's
    meta frame. Returns the :class:`EgressStreamReader` once response
    headers are known (mirroring connect-phase semantics).

    Raises:
        EgressUnavailable: host not whitelisted / no agent / no meta frame
            within the connect window.
    """
    _check_target(url, target, loopback_port)
    candidates = ([agent_id] if agent_id is not None
                  else route_candidates(url, user_id=user_id))
    from lib.desktop import enqueue_desktop_command
    last_err = None
    for cand in candidates:
        if cand == 'direct':
            break  # caller handles the direct path; never stream-wrap it
        cmd_id = uuid.uuid4().hex
        params = {
            'url': url,
            'method': (method or 'POST').upper(),
            'headers': dict(headers or {}),
            'body_b64': base64.b64encode(body or b'').decode('ascii'),
            'timeout_ms': _EGRESS_STREAM_TTL_S * 1000,
            'proxy_mode': 'env',
            'stream_id': cmd_id,
            'target': target,
        }
        enq_id, err = enqueue_desktop_command(
            'egress_http_stream', params,
            target_agent_id=cand, user_id=user_id,
            cmd_id=cmd_id, ttl=_EGRESS_STREAM_TTL_S)
        if err:
            last_err = err
            logger.info('[Egress] stream enqueue to agent %s failed: %s '
                        '— trying next candidate', cand[:8], err)
            continue
        reader = EgressStreamReader(cmd_id, cand, user_id)
        try:
            reader.wait_headers()
        except EgressUnavailable as e:
            # Pre-meta failure is safe to fail over ONLY when zero frames
            # arrived: the upstream was never reached. Once any frame
            # landed, retrying could double-bill the subscription — raise.
            if reader._seq != 0:
                raise
            last_err = e
            logger.info('[Egress] stream via agent %s produced no frames '
                        '(%s) — trying next candidate', cand[:8], e)
            cancel_stream(cmd_id, cand, user_id)
            continue
        _note_success(cand)
        logger.info('%s[Egress] stream %s %s via agent %s (cmd=%s)',
                    log_prefix, params['method'], urlparse(url).hostname,
                    cand[:8], cmd_id[:8])
        return reader
    raise EgressUnavailable(
        f'all egress candidates failed (last: {last_err})')


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

_probe_bg_lock = threading.Lock()
_probe_bg_fired: set = set()

#: The status warm-up probe must hit the provider's REAL API endpoint, not
#: its web root: ``chatgpt.com/`` sits behind Cloudflare bot-guard and
#: answers 403 to a bare POST even over a healthy proxy, which classified as
#: ``geo_blocked`` and — the cache being host-keyed and shared with
#: real-traffic routing — blocked every Codex call for the full TTL
#: (2026-08-10 incident: 5 min of EndpointUnreachableError while
#: ``/backend-api/codex/responses`` itself answered 401 = reachable).
#: Hosts absent here fall back to ``https://<host>/``.
_BG_PROBE_URL = {
    'chatgpt.com': 'https://chatgpt.com/backend-api/codex/responses',
}


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
            url = _BG_PROBE_URL.get(host, f'https://{host}/')
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

    NEVER probes inline — reads the stable or short-lived failure cache only.
    """
    host = (host or '').lower()
    from lib.subscription_routes import manager as route_manager
    route_status = route_manager.status()
    preferred_route_id = route_status.get('preferred', {}).get(host, '')
    route_rows = {
        route_id: dict(row)
        for route_id, row in route_status.get('routes', {}).get(host, {}).items()
    }
    preferred_route_label = ''
    preferred_route_mode = ''
    # RouteManager intentionally stores credential-free ids only. Rehydrate
    # the equally credential-free label/mode from the current proxy topology
    # so Settings can say whether the working SERVER path is direct or a
    # named proxy. Never expose proxy_url (it may contain credentials).
    try:
        from lib.proxy import subscription_route_specs
        probe_url = _BG_PROBE_URL.get(host, f'https://{host}/')
        for route in subscription_route_specs(probe_url):
            if route.route_id in route_rows:
                route_rows[route.route_id].update({
                    'label': route.label,
                    'mode': route.mode,
                })
            if route.route_id == preferred_route_id:
                preferred_route_label = route.label
                preferred_route_mode = route.mode
    except Exception as e:
        logger.debug('[Egress] route diagnostics failed for %s: %s', host, e)
    diagnostics = {
        'preferred_server_route': preferred_route_id,
        'preferred_server_route_label': preferred_route_label,
        'preferred_server_route_mode': preferred_route_mode,
        'server_routes': route_rows,
    }
    verdict = _cached_probe_verdict(host)
    if not verdict:
        _spawn_background_probe(host)
        return {'state': 'unknown', 'verdict': '', 'agents': [],
                **diagnostics}
    if verdict == 'ok':
        return {'state': 'direct', 'verdict': verdict, 'agents': [],
                **diagnostics}
    from lib.desktop import online_agents
    all_agents = online_agents()
    capable = [a for a in all_agents
               if (a.get('capabilities') or {}).get('egress')
               and (not user_id or (a.get('user_id') or '') == user_id)]
    if capable:
        return {'state': 'agent', 'verdict': verdict,
                'agents': [{'agent_id': a['agent_id'], 'name': a.get('name', '')}
                           for a in capable], **diagnostics}
    scoped_any = [a for a in all_agents
                  if not user_id or (a.get('user_id') or '') == user_id]
    if scoped_any:
        return {'state': 'agent_no_capability', 'verdict': verdict,
                'agents': [{'agent_id': a['agent_id'], 'name': a.get('name', '')}
                           for a in scoped_any], **diagnostics}
    return {'state': 'unavailable', 'verdict': verdict, 'agents': [],
            **diagnostics}


def egress_http(url: str, *, method: str = 'POST', headers: dict = None,
                body: bytes = b'', timeout: float = 30,
                user_id: str = '', agent_id: str = None,
                target: str = 'subscription',
                loopback_port: int = 0) -> EgressResponse:
    """Execute one HTTP request through the caller's desktop agent.

    Whitelist → agent selection → bridge command (TTL 120s) → adapt the
    agent result into :class:`EgressResponse`. ``agent_id`` pins a single
    candidate (loopback adapter relays MUST pin — a different machine
    hosts a different adapter with different credentials). ``target``
    selects the whitelist class (``subscription`` public hosts vs
    ``loopback`` agent-local adapter port); the agent re-enforces it.

    Raises:
        EgressUnavailable: host not whitelisted, no agent, agent offline /
            timed out, or the agent hit a network error.
    """
    _check_target(url, target, loopback_port)
    from lib.desktop import send_desktop_command
    params = {
        'url': url,
        'method': (method or 'POST').upper(),
        'headers': dict(headers or {}),
        'body_b64': base64.b64encode(body or b'').decode('ascii'),
        'timeout_ms': int(min(max(timeout, 1), 60) * 1000),
        'proxy_mode': 'env',
        'target': target,
    }
    last_err = None
    candidates = ([agent_id] if agent_id is not None
                  else route_candidates(url, user_id=user_id))
    for agent_id in candidates:
        if agent_id == 'direct':
            break  # caller's transport handles direct; egress_http is the detour
        logger.info('[Egress] routing %s %s via agent %s (user=%s)',
                    params['method'], urlparse(url).hostname, agent_id[:8],
                    user_id or '(legacy)')
        result, error = send_desktop_command(
            'egress_http', params,
            timeout=min(timeout + 15, _EGRESS_HTTP_TTL_S),
            target_agent_id=agent_id, user_id=user_id,
            ttl=_EGRESS_HTTP_TTL_S)
        if error or result is None:
            last_err = error or 'no result'
            logger.info('[Egress] agent %s bridge failure (%s) — trying '
                        'next candidate', agent_id[:8], last_err)
            continue
        status = int(result.get('status') or 0)
        if status == 0:
            # The agent's own network failed — another machine's may not.
            last_err = result.get('error') or 'unknown'
            logger.info('[Egress] agent %s network failure (%s) — trying '
                        'next candidate', agent_id[:8], last_err)
            continue
        try:
            content = base64.b64decode(result.get('body_b64') or '')
        except Exception as e:
            last_err = f'undecodable body: {e}'
            logger.info('[Egress] agent %s undecodable body (%s) — trying '
                        'next candidate', agent_id[:8], e)
            continue
        _note_success(agent_id)
        return EgressResponse(
            status=status,
            headers=result.get('headers') or {},
            content=content,
            elapsed_ms=int(result.get('elapsed_ms') or 0),
        )
    raise EgressUnavailable(
        f'all egress candidates failed (last: {last_err})')
