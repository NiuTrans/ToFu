"""lib/netpath.py — Adaptive per-host route selection (N-path pool).

Answers one question per host the app talks to: **which route — direct,
the environment proxy, or any proxy-pool entry — is fastest / most
reliable right now?** — and pins the winner, per host, with no
domain-specific rules anywhere.

Route ids (shared convention with ``lib.subscription_routes``):

  ``direct``     — no proxy at all
  ``env``        — the deployment's environment proxy (``http_proxy`` env)
  ``pool:<id>``  — one entry of the ``lib.proxy`` proxy pool

The route set available for a host comes from a **route provider**
registered by ``lib.proxy`` at import time
(:func:`register_route_provider`).  With no provider (minimal contexts,
unit tests) the legacy pair — direct plus the env proxy — is used.

Three signal sources feed a per-host scorer:

1. **Passive outcomes** — every real request routed through
   :func:`lib.proxy.proxies_for` reports success/failure + latency via
   :func:`report_outcome` (hooked in the LLM transports and
   ``lib.http_client``); ``run_command`` does the same for subprocess
   (curl/pip/npm) traffic via ``lib.project_mod.run_net``.  Real traffic
   is ground truth.
2. **Active probing** — a daemon thread periodically fetches
   ``scheme://host/`` over EVERY available route (lightweight: headers
   only, ~3s timeout) so latency comparisons exist even when traffic is
   quiet and so a healed route is discovered without waiting for a
   user-visible failure.
3. **Persistence** — learned state survives restarts via
   ``data/config/netpath.json`` (schema v2; v1 files migrate on load —
   the old singleton ``proxy`` path becomes the ``env`` route).

Decision rules (anti-flap by construction):

- A route becomes *bad* after ``_FAIL_THRESHOLD`` consecutive failures;
  a single success redeems it.
- A bad current route is abandoned for the best measured healthy route,
  else any healthy route; all routes bad → undecided → fall back to the
  deployment default (env proxy behaviour), which is the last hope anyway.
- Latency switches require the challenger to have ``_MIN_SAMPLES``
  measurements and be ``_LAT_MARGIN`` (25%) faster — hysteresis so the
  pin doesn't oscillate on jitter.
- A pinned route that disappears from the topology (pool entry removed)
  is released back to undecided.

Precedence in ``lib.proxy.proxies_for``: explicit user config (always-
bypass hosts, registered no-proxy hosts, bypass-domain suffixes) wins
over learned decisions; learned decisions win over the env default.

Env knobs:
  ``TOFU_NETPATH``            on/off master switch (default: on)
  ``TOFU_NETPATH_INTERVAL``   probe round interval seconds (default: 180)
  ``TOFU_NETPATH_TIMEOUT``    per-probe connect/read timeout (default: 3)
  ``TOFU_NETPATH_ROUTES_TTL`` seconds a host's route list is cached (5)
"""

from __future__ import annotations

import ipaddress
import os
import random
import threading
import time
from copy import deepcopy
from urllib.parse import urlparse

from lib.config_dir import config_path
from lib.json_store import read_json, write_json_atomic
from lib.log import get_logger

logger = get_logger(__name__)

__all__ = [
    'note_url', 'decide', 'report_outcome', 'probe_host',
    'start_prober', 'stop_prober', 'status_summary', 'host_status',
    'register_route_provider', 'reset_proxy_stats', 'reset_for_test',
]

# ── Tunables ─────────────────────────────────────────────────────
_FAIL_THRESHOLD = 2        # consecutive failures before a route is "bad"
_LAT_MARGIN = 0.75         # challenger must be ≤75% of incumbent's EWMA
_MIN_SAMPLES = 2           # measurements required before latency switch
_EWMA_ALPHA = 0.3          # latency smoothing factor
_MAX_HOSTS = 64            # LRU cap on tracked hosts
_HOST_TTL = 24 * 3600      # stop probing hosts not seen for this long
_SAVE_THROTTLE = 30        # seconds between disk writes
_MAX_LAT_MS = 30_000       # discard insane latency outliers

try:
    _PROBE_INTERVAL = float(os.environ.get('TOFU_NETPATH_INTERVAL', '180'))
    if _PROBE_INTERVAL <= 0:
        _PROBE_INTERVAL = 180.0
except (ValueError, TypeError) as _e:
    logger.debug('<module>: unparseable/unexpected type (%s)', _e)
    _PROBE_INTERVAL = 180.0
try:
    _PROBE_TIMEOUT = float(os.environ.get('TOFU_NETPATH_TIMEOUT', '3'))
    if _PROBE_TIMEOUT <= 0:
        _PROBE_TIMEOUT = 3.0
except (ValueError, TypeError) as _e:
    logger.debug('<module>: unparseable/unexpected type (%s)', _e)
    _PROBE_TIMEOUT = 3.0
try:
    _ROUTES_TTL = float(os.environ.get('TOFU_NETPATH_ROUTES_TTL', '5'))
    if _ROUTES_TTL < 0:
        _ROUTES_TTL = 5.0
except (ValueError, TypeError) as _e:
    logger.debug('<module>: unparseable/unexpected type (%s)', _e)
    _ROUTES_TTL = 5.0

_STORE_PATH = config_path('netpath.json')
_STORE_VERSION = 2


def _enabled() -> bool:
    return os.environ.get('TOFU_NETPATH', 'on').strip().lower() not in (
        '0', 'off', 'false', 'no')


# Hosts netpath must never track or probe: loopback-style names and ANY IP
# literal (v4/v6). Self-hosted endpoints are commonly addressed by raw IP or
# localhost; probing them over the corporate proxy is meaningless (the proxy
# cannot reach them), and one jittery direct probe could pin them onto the
# proxy path and black-hole the local model server.
_EXEMPT_HOSTNAMES = frozenset({'localhost', 'localhost.localdomain'})


def _is_ip_literal(host: str) -> bool:
    """True when *host* is an IPv4/IPv6 literal rather than a DNS name."""
    try:
        ipaddress.ip_address(host)
        return True
    except ValueError as _e:
        logger.debug('is ip literal: unparseable (%s)', _e)
        return False


def _is_exempt_host(host: str) -> bool:
    """True for hosts netpath must leave alone: localhost & IP literals."""
    h = (host or '').lower()
    return h in _EXEMPT_HOSTNAMES or _is_ip_literal(h)


def _proxy_url() -> 'str | None':
    """The env-proxy URL — the ``env`` route probes should use."""
    return (os.environ.get('https_proxy')
            or os.environ.get('HTTPS_PROXY')
            or os.environ.get('http_proxy')
            or os.environ.get('HTTP_PROXY')
            or None)


def _new_path() -> dict:
    return {
        'ewma_ms': None,   # smoothed latency, None = never measured
        'samples': 0,      # successful measurements
        'fails': 0,        # CONSECUTIVE failures (reset by any success)
        'last_ok': 0.0,
        'last_fail': 0.0,
    }


def _new_state(host: str, sample_url: str) -> dict:
    return {
        'host': host,
        'sample_url': sample_url,
        'last_seen': time.time(),
        'decision': None,        # route id | None (undecided)
        'effective': None,       # route the last decide() actually resolved to
        'decision_since': 0.0,
        'routes': ['direct'],    # available route ids, provider order
        'routes_ts': 0.0,        # monotonic ts of last route-list refresh
        'paths': {'direct': _new_path()},
    }


_lock = threading.Lock()
_states: 'dict[str, dict]' = {}
_dirty = False
_generation = 0
_last_save = 0.0

_prober_thread: 'threading.Thread | None' = None
_prober_stop = threading.Event()

# Route provider: fn(host) -> iterable[(route_id, requests-proxies-dict)].
# Registered by lib.proxy at import time; None → legacy direct + env pair.
_route_provider = None


def register_route_provider(fn) -> None:
    """Register the callable that enumerates a host's available routes.

    *fn(host)* must return ``[(route_id, proxies_dict), ...]`` in failover
    preference order, always including ``direct``.  Called under netpath's
    lock on the decide hot path (TTL-cached) and lock-free from the prober
    thread; it must never call back into netpath.
    """
    global _route_provider
    _route_provider = fn


def _routes_for(host: str) -> list:
    """``[(route_id, proxies_dict)]`` for *host* — provider or legacy pair."""
    provider = _route_provider
    if provider is not None:
        try:
            routes = [(str(rid), dict(proxies))
                      for rid, proxies in (provider(host) or ())
                      if rid and isinstance(proxies, dict)]
            if any(rid == 'direct' for rid, _ in routes):
                return routes
        except Exception as e:
            logger.debug('[Netpath] route provider failed for %s: %s', host, e)
    routes = [('direct', {'no_proxy': '*'})]
    env_proxy = _proxy_url()
    if env_proxy:
        routes.append(('env', {'http': env_proxy, 'https': env_proxy}))
    return routes


def _path(st: dict, route_id: str) -> dict:
    """Get-or-create the scorer slot for *route_id* in *st*."""
    path = st['paths'].get(route_id)
    if path is None:
        path = _new_path()
        st['paths'][route_id] = path
    return path


def _refresh_routes(st: dict, force: bool = False) -> None:
    """Re-enumerate *st*'s available routes (TTL-cached). Caller holds lock."""
    now = time.monotonic()
    if not force and now - st['routes_ts'] < _ROUTES_TTL:
        return
    st['routes_ts'] = now
    ids = [rid for rid, _ in _routes_for(st['host'])]
    if ids:
        st['routes'] = ids
    for rid in st['routes']:
        _path(st, rid)


# ═════════════════════════════════════════════════════════════
#  Registration + decision (hot path — called per request)
# ═════════════════════════════════════════════════════════════

def note_url(url: str) -> None:
    """Register *url*'s host as worth managing. Cheap and idempotent."""
    if not _enabled():
        return
    try:
        parsed = urlparse(url)
        host = (parsed.hostname or '').lower()
        if not host or _is_exempt_host(host):
            return
        origin = '%s://%s/' % (parsed.scheme or 'https',
                               parsed.netloc.split('@')[-1])
    except Exception as _e:
        logger.debug('note url: failed (%s)', _e)
        return
    with _lock:
        st = _states.get(host)
        if st is None:
            if len(_states) >= _MAX_HOSTS:
                # LRU evict: drop the stalest host.
                stale = min(_states, key=lambda h: _states[h]['last_seen'])
                _states.pop(stale, None)
            st = _new_state(host, origin)
            _states[host] = st
            _refresh_routes(st, force=True)
            logger.debug('[Netpath] Tracking host: %s', host)
        st['last_seen'] = time.time()


def decide(host: str) -> 'str | None':
    """Return the pinned route id for *host*: 'direct', 'env',
    'pool:<id>', or None.

    None means "no learned preference — follow the deployment default".
    Also records the *effective* route (decision or env default) so a
    later :func:`report_outcome` can be attributed correctly.
    """
    if not _enabled():
        return None
    host = (host or '').lower()
    if _is_exempt_host(host):
        return None
    with _lock:
        st = _states.get(host)
        if st is None:
            return None
        _refresh_routes(st)
        # Re-derive the pin on read: a released pin (topology change wiped
        # a route) or freshly arrived probe measurements must take effect
        # without waiting for the next real-traffic outcome.
        _reevaluate(st)
        eff = st['decision']
        if eff is None:
            eff = 'env' if _proxy_url() else 'direct'
        st['effective'] = eff
        return st['decision']


def _is_bad(path: dict) -> bool:
    return path['fails'] >= _FAIL_THRESHOLD


def _reevaluate(st: dict) -> None:
    """Recompute st['decision'] from current route stats. Caller holds lock."""
    paths = st['paths']
    avail = [r for r in (st.get('routes') or ['direct']) if r in paths]
    # 'direct' is always a legal route even if never enumerated.
    if not avail:
        avail = ['direct']
    healthy = [r for r in avail if not _is_bad(paths[r])]
    measured_healthy = [r for r in healthy
                        if paths[r]['ewma_ms'] is not None]
    cur = st['decision']
    new = cur

    if cur is not None and (cur not in avail or _is_bad(paths[cur])):
        # The pin went bad — or vanished from the topology (pool entry
        # removed / env proxy unset). Release it and pick the best
        # alternative: best measured healthy route, else any healthy route,
        # else undecided (deployment default is the last hope).
        if cur in avail:
            others = [r for r in healthy if r != cur]
            measured = [r for r in others
                        if paths[r]['ewma_ms'] is not None]
            if measured:
                new = min(measured, key=lambda r: paths[r]['ewma_ms'])
            elif others:
                new = others[0]
            else:
                new = None
        else:
            new = None
    if new is None:
        # No valid pin: adopt the best measured healthy route when one
        # exists, otherwise stay undecided.
        if measured_healthy:
            new = min(measured_healthy,
                      key=lambda r: paths[r]['ewma_ms'])
    elif new in measured_healthy:
        # Latency contest among healthy, measured routes: a challenger must
        # beat the incumbent by _LAT_MARGIN with _MIN_SAMPLES — hysteresis
        # so the pin doesn't oscillate on jitter.
        best = min(measured_healthy, key=lambda r: paths[r]['ewma_ms'])
        if best != new and paths[best]['samples'] >= _MIN_SAMPLES:
            cur_lat = paths[new]['ewma_ms']
            if (cur_lat is None
                    or paths[best]['ewma_ms'] < cur_lat * _LAT_MARGIN):
                new = best

    if new != cur:
        st['decision'] = new
        st['decision_since'] = time.time()
        logger.info('[Netpath] %s: route %s → %s (%s)',
                    st['host'], cur or 'default', new or 'default',
                    ' '.join('%s=%s' % (r, _fmt_path(paths[r]))
                             for r in avail))


def _fmt_path(path: dict) -> str:
    if path['ewma_ms'] is None and not path['fails']:
        return 'unmeasured'
    lat = '%.0fms' % path['ewma_ms'] if path['ewma_ms'] is not None else '?'
    return '%s%s' % (lat, ' BAD' if _is_bad(path) else '')


# ═════════════════════════════════════════════════════════════
#  Outcome reporting (passive feed + prober feed)
# ═════════════════════════════════════════════════════════════

def _valid_route_id(route_id) -> bool:
    return (route_id == 'direct' or route_id == 'env'
            or (isinstance(route_id, str)
                and route_id.startswith('pool:') and len(route_id) > 5))


def report_outcome(url: str, ok: bool, latency_ms: 'float | None' = None,
                   *, path: 'str | None' = None) -> None:
    """Attribute a real (or probe) request outcome to a route.

    ``path`` forces attribution to a route id ('direct' / 'env' /
    'pool:<id>') — used by the prober, which chooses the route itself,
    and by run_command's subprocess feed.  Real traffic omits it and the
    outcome is attributed to the effective route the request actually
    used.  Never raises — transports call this on their hot path.
    """
    if not _enabled():
        return
    global _dirty, _generation
    try:
        host = (urlparse(url).hostname or '').lower()
    except Exception as _e:
        logger.debug('report outcome: failed (%s)', _e)
        return
    if not host:
        return
    now = time.time()
    with _lock:
        st = _states.get(host)
        if st is None:
            return
        path_name = path if _valid_route_id(path) else st.get('effective')
        if not _valid_route_id(path_name):
            path_name = 'env' if _proxy_url() else 'direct'
        if path_name not in (st.get('routes') or []):
            # An outcome from a route the provider no longer lists (e.g. a
            # just-removed pool entry) still counts: it existed when used.
            st['routes'].append(path_name)
        route = _path(st, path_name)
        if ok:
            route['fails'] = 0
            route['last_ok'] = now
            if latency_ms is not None and 0 < latency_ms <= _MAX_LAT_MS:
                route['samples'] += 1
                if route['ewma_ms'] is None:
                    route['ewma_ms'] = float(latency_ms)
                else:
                    a = _EWMA_ALPHA
                    route['ewma_ms'] = a * latency_ms + (1 - a) * route['ewma_ms']
        else:
            route['fails'] += 1
            route['last_fail'] = now
        _reevaluate(st)
        _dirty = True
        _generation += 1
    _maybe_save()


# ═════════════════════════════════════════════════════════════
#  Active probing
# ═════════════════════════════════════════════════════════════

def _probe_once(url: str, proxies: dict) -> 'tuple[bool, float | None]':
    """Fetch *url* headers-only over one route. Any HTTP status = route works."""
    import requests  # local import: keep module import light for non-server use
    t0 = time.monotonic()
    try:
        resp = requests.get(
            url, timeout=(_PROBE_TIMEOUT, _PROBE_TIMEOUT),
            proxies=proxies, stream=True, allow_redirects=False)
        resp.close()
        return (True, (time.monotonic() - t0) * 1000.0)
    except Exception as _e:
        logger.debug('probe once: failed (%s)', _e)
        return (False, None)


def probe_host(host: str) -> None:
    """Probe every available route for one host and feed the scorer."""
    host = (host or '').lower()
    with _lock:
        st = _states.get(host)
        url = st['sample_url'] if st else None
    if not url:
        return
    for route_id, proxies in _routes_for(host):
        ok, lat = _probe_once(url, proxies)
        report_outcome(url, ok, lat, path=route_id)


def _probe_round() -> None:
    with _lock:
        now = time.time()
        hosts = [h for h, st in _states.items()
                 if now - st['last_seen'] < _HOST_TTL]
    for host in hosts:
        if _prober_stop.is_set():
            return
        try:
            probe_host(host)
        except Exception as e:
            logger.debug('[Netpath] probe failed for %s: %s', host, e)
        # Small jitter between hosts so a round never bursts.
        _prober_stop.wait(random.uniform(0.1, 0.4))
    _save()


def start_prober(interval: 'float | None' = None) -> bool:
    """Start the background probe loop (idempotent). Returns True if running."""
    global _prober_thread
    if not _enabled():
        logger.debug('[Netpath] Disabled via TOFU_NETPATH — prober not started')
        return False
    with _lock:
        if _prober_thread is not None and _prober_thread.is_alive():
            return True
        _prober_stop.clear()
        period = interval or _PROBE_INTERVAL
        _prober_thread = threading.Thread(
            target=_prober_loop, args=(period,),
            name='netpath-prober', daemon=True)
        _prober_thread.start()
    logger.info('[Netpath] Prober started (interval %.0fs, timeout %.1fs)',
                period, _PROBE_TIMEOUT)
    return True


def _prober_loop(period: float) -> None:
    # First round soon after boot (paths are unknown — data is most valuable
    # early), then settle into the regular cadence with ±20% jitter.
    if _prober_stop.wait(10):
        return
    while not _prober_stop.is_set():
        try:
            _probe_round()
        except Exception as e:
            logger.debug('[Netpath] probe round failed: %s', e)
        _prober_stop.wait(period * random.uniform(0.8, 1.2))


def stop_prober() -> None:
    """Stop the background probe loop (test helper / shutdown)."""
    global _prober_thread
    _prober_stop.set()
    t = _prober_thread
    if t is not None and t.is_alive():
        t.join(timeout=5)
    _prober_thread = None


# ═════════════════════════════════════════════════════════════
#  Persistence + status
# ═════════════════════════════════════════════════════════════

def _maybe_save() -> None:
    global _last_save
    now = time.time()
    if now - _last_save >= _SAVE_THROTTLE:
        _save()


def _save() -> None:
    global _dirty, _last_save
    with _lock:
        if not _dirty:
            return
        generation = _generation
        payload = {
            'version': _STORE_VERSION,
            'saved_at': time.time(),
            # The serializer runs after releasing _lock.  Snapshot nested path
            # dicts too, otherwise report_outcome can mutate the payload while
            # json is walking it and persist a torn combination of counters.
            'hosts': deepcopy(list(_states.values())),
        }
    try:
        write_json_atomic(_STORE_PATH, payload, fsync=False, indent=None)
    except Exception as e:
        # Keep the generation dirty so a later report/probe retries.  The old
        # code cleared it before I/O, permanently forgetting the failure.
        with _lock:
            _dirty = True
            _last_save = time.time()
        logger.warning('[Netpath] save failed; state remains dirty: %s', e)
        return
    with _lock:
        # A report may have landed while the snapshot was being serialized.
        # Clear only the exact generation that reached disk.
        if _generation == generation:
            _dirty = False
        _last_save = time.time()


def _migrate_v1_host(st: dict) -> dict:
    """Translate a v1 persisted host (paths {'direct','proxy'}) to v2."""
    paths = st.get('paths')
    if isinstance(paths, dict) and 'proxy' in paths and 'env' not in paths:
        paths['env'] = paths.pop('proxy')
    if st.get('decision') == 'proxy':
        st['decision'] = 'env'
    return st


def _load() -> None:
    global _dirty, _generation
    payload = read_json(_STORE_PATH, default=None)
    if not isinstance(payload, dict):
        return
    version = payload.get('version')
    if version not in (1, _STORE_VERSION):
        return
    now = time.time()
    restored = 0
    with _lock:
        hosts = payload.get('hosts')
        if not isinstance(hosts, list):
            hosts = []
        for st in hosts:
            if not isinstance(st, dict):
                continue
            try:
                if version == 1:
                    st = _migrate_v1_host(st)
                host = (st.get('host') or '').lower()
                if not host or host in _states or _is_exempt_host(host):
                    continue
                sample_url = st.get('sample_url') or ''
                parsed = urlparse(sample_url)
                if (parsed.scheme not in ('http', 'https')
                        or (parsed.hostname or '').lower() != host
                        or parsed.username is not None
                        or parsed.password is not None):
                    sample_url = 'https://%s/' % host
                # Restore only measurement data + decision; timestamps that
                # drive TTL/LRU are refreshed so stale hosts age out.  The
                # route list is NOT restored — the provider re-enumerates it
                # on the next decide()/probe (pool entries may be gone).
                fresh = _new_state(host, sample_url)
                decision = st.get('decision')
                fresh['decision'] = (decision if _valid_route_id(decision)
                                     else None)
                fresh['last_seen'] = now
                saved_paths = st.get('paths')
                if not isinstance(saved_paths, dict):
                    saved_paths = {}
                for name, src in saved_paths.items():
                    if not _valid_route_id(name):
                        continue
                    if not isinstance(src, dict):
                        src = {}
                    dst = _path(fresh, name)
                    latency = src.get('ewma_ms')
                    dst['ewma_ms'] = (float(latency) if latency is not None
                                      else None)
                    dst['samples'] = max(0, int(src.get('samples') or 0))
                    dst['fails'] = max(0, int(src.get('fails') or 0))
                _states[host] = fresh
                restored += 1
            except (TypeError, ValueError, OverflowError) as error:
                logger.debug('[Netpath] skipped malformed host state: %s',
                             error)
        _dirty = False
        _generation = 0
    logger.info('[Netpath] Restored %d host(s) from disk (schema v%s)',
                restored, version)


def status_summary() -> dict:
    """Compact per-host view for the Settings UI / diagnostics."""
    with _lock:
        out = {}
        for host, st in _states.items():
            routes = {}
            for rid, path in st['paths'].items():
                routes[rid] = {
                    'ms': _round(path['ewma_ms']),
                    'fails': path['fails'],
                    'samples': path['samples'],
                }
            env_path = st['paths'].get('env', _new_path())
            out[host] = {
                'decision': st['decision'] or 'default',
                # Legacy flat keys (env route == the old singleton 'proxy').
                'direct_ms': _round(st['paths'].get('direct',
                                                    _new_path())['ewma_ms']),
                'proxy_ms': _round(env_path['ewma_ms']),
                'direct_fails': st['paths'].get('direct',
                                                _new_path())['fails'],
                'proxy_fails': env_path['fails'],
                # v2: every route, including proxy-pool entries.
                'routes': routes,
            }
        return {'enabled': _enabled(), 'hosts': out}


def _round(v):
    return None if v is None else round(float(v), 1)


def host_status(host: str) -> 'dict | None':
    """Per-route health for ONE host (run_command diagnosis blocks).

    Returns ``{route_id: {'ms', 'fails', 'bad', 'available'}}`` plus the
    current ``decision``, or None when the host is not tracked.  Never
    raises, never probes — a pure read of learned state.
    """
    host = (host or '').lower()
    with _lock:
        st = _states.get(host)
        if st is None:
            return None
        avail = st.get('routes') or ['direct']
        routes = {}
        for rid, path in st['paths'].items():
            routes[rid] = {
                'ms': _round(path['ewma_ms']),
                'fails': path['fails'],
                'bad': _is_bad(path),
                'available': rid in avail,
            }
        return {'decision': st['decision'], 'routes': routes}


def reset_proxy_stats(routes: 'tuple | None' = None) -> None:
    """Invalidate proxied-route measurements after a topology change.

    ``routes`` selects the route ids to wipe: 'env' for the environment
    proxy, 'pool:' for every proxy-pool route.  None wipes ALL non-direct
    routes (legacy behaviour).  A scoped wipe matters at boot: loading the
    persisted pool must not throw away the env route's learned stats (and
    vice versa).  Direct-route measurements always survive — a proxy
    change says nothing about them.  A pin on a wiped route is released.
    """
    global _dirty, _generation

    def _wiped(rid):
        if rid == 'direct':
            return False
        if routes is None:
            return True
        for sel in routes:
            if sel == 'pool:' and rid.startswith('pool:'):
                return True
            if rid == sel:
                return True
        return False

    changed = False
    with _lock:
        for st in _states.values():
            for rid in list(st['paths']):
                if not _wiped(rid):
                    continue
                old = st['paths'][rid]
                if (old['ewma_ms'] is not None or old['samples']
                        or old['fails'] or st['decision'] == rid):
                    changed = True
                st['paths'][rid] = _new_path()
            if st['decision'] and _wiped(st['decision']):
                st['decision'] = None
        if changed:
            _dirty = True
            _generation += 1
    if changed:
        _save()
    logger.info('[Netpath] Proxy stats reset (scope=%s)',
                'all' if routes is None else ','.join(routes))


def reset_for_test() -> None:
    """Clear all learned state and stop the prober. Test-only.

    The registered route provider is deliberately kept: it is wiring, not
    learned state.
    """
    global _dirty, _generation, _last_save
    stop_prober()
    with _lock:
        _states.clear()
        _dirty = False
        _generation = 0
        _last_save = 0.0


_load()
