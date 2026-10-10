"""lib/netpath.py — Adaptive direct-vs-proxy path selection.

Answers one question per host the app talks to: **is the direct path or
the HTTP-proxy path faster / more reliable right now?** — and pins the
winner, per host, with no domain-specific rules anywhere.

Three signal sources feed a per-host scorer:

1. **Passive outcomes** — every real request routed through
   :func:`lib.proxy.proxies_for` reports success/failure + latency via
   :func:`report_outcome` (hooked in the LLM transports and
   ``lib.http_client``). Real traffic is ground truth.
2. **Active probing** — a daemon worker fetches ``scheme://host/`` over
   paths whose bounded adaptive deadline is due (headers only, ~3s timeout).
   Stable or repeatedly failed paths back off; a passive request failure wakes
   the worker immediately so recovery does not wait for the periodic ceiling.
3. **Persistence** — learned state survives restarts via
   ``data/config/netpath.json``.

Decision rules (anti-flap by construction):

- A path becomes *bad* after ``_FAIL_THRESHOLD`` consecutive failures;
  a single success redeems it.
- A bad current path is abandoned for the other path as soon as the
  other is not known-bad.
- Latency switches require the challenger to have ``_MIN_SAMPLES``
  measurements and be ``_LAT_MARGIN`` (25%) faster — hysteresis so the
  pin doesn't oscillate on jitter.
- Both paths bad → undecided → fall back to the deployment default
  (env proxy behaviour), which is the last hope anyway.

Precedence in ``lib.proxy.proxies_for``: explicit user config (always-
bypass hosts, registered no-proxy hosts, bypass-domain suffixes) wins
over learned decisions; learned decisions win over the env default.

Env knobs:
  ``TOFU_NETPATH``          on/off master switch (default: on)
  ``TOFU_NETPATH_INTERVAL`` probe round interval seconds (default: 180)
  ``TOFU_NETPATH_MAX_INTERVAL`` stable-path ceiling seconds (default: 3600)
  ``TOFU_NETPATH_TIMEOUT``  connect/read timeout (default: 3, hard max: 30)
"""

from __future__ import annotations

import ipaddress
import math
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
    'start_prober', 'stop_prober', 'status_summary',
    'reset_proxy_stats', 'reset_for_test',
]

# ── Tunables ─────────────────────────────────────────────────────
_FAIL_THRESHOLD = 2        # consecutive failures before a path is "bad"
_LAT_MARGIN = 0.75         # challenger must be ≤75% of incumbent's EWMA
_MIN_SAMPLES = 2           # measurements required before latency switch
_EWMA_ALPHA = 0.3          # latency smoothing factor
_MAX_HOSTS = 64            # LRU cap on tracked hosts
_HOST_TTL = 24 * 3600      # stop probing hosts not seen for this long
_SAVE_THROTTLE = 30        # seconds between disk writes
_MAX_LAT_MS = 30_000       # discard insane latency outliers
_MIN_PROBE_INTERVAL = 30.0
_HARD_MAX_PROBE_INTERVAL = 6 * 3600.0
_MAX_PROBE_FAILURES = 16


def _bounded_probe_interval(value: object, default: float = 180.0) -> float:
    """Keep explicit probe cadence inside the personal-computer budget."""
    try:
        seconds = float(value)
    except (ValueError, TypeError):
        seconds = default
    if not math.isfinite(seconds) or seconds <= 0:
        seconds = default
    return min(_HARD_MAX_PROBE_INTERVAL,
               max(_MIN_PROBE_INTERVAL, seconds))


_PROBE_INTERVAL = _bounded_probe_interval(
    os.environ.get('TOFU_NETPATH_INTERVAL', '180'))
_PROBE_MAX_INTERVAL = max(
    _PROBE_INTERVAL,
    _bounded_probe_interval(
        os.environ.get('TOFU_NETPATH_MAX_INTERVAL', '3600'),
        default=3600.0,
    ),
)
try:
    _PROBE_TIMEOUT = float(os.environ.get('TOFU_NETPATH_TIMEOUT', '3'))
    if not math.isfinite(_PROBE_TIMEOUT) or _PROBE_TIMEOUT <= 0:
        _PROBE_TIMEOUT = 3.0
    _PROBE_TIMEOUT = min(30.0, max(0.1, _PROBE_TIMEOUT))
except (ValueError, TypeError) as _e:
    logger.debug('<module>: unparseable/unexpected type (%s)', _e)
    _PROBE_TIMEOUT = 3.0

_STORE_PATH = config_path('netpath.json')
_STORE_VERSION = 1


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
    """The proxy URL probes should use, straight from the environment."""
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
        # Active-probe scheduling is bounded per path and persisted with the
        # measurement state. Passive traffic can postpone redundant probes or
        # pull the alternate path forward after a real failure.
        'last_probe': 0.0,
        'probe_failures': 0,
        'next_probe': 0.0,
    }


def _new_state(host: str, sample_url: str) -> dict:
    return {
        'host': host,
        'sample_url': sample_url,
        'last_seen': time.time(),
        'decision': None,        # 'direct' | 'proxy' | None (undecided)
        'effective': None,       # path the last decide() actually resolved to
        'decision_since': 0.0,
        'paths': {'direct': _new_path(), 'proxy': _new_path()},
    }


_lock = threading.Lock()
_states: 'dict[str, dict]' = {}
_dirty = False
_generation = 0
_last_save = 0.0

_prober_thread: 'threading.Thread | None' = None
_prober_stop = threading.Event()
_prober_wake = threading.Event()


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
    should_wake_prober = False
    now = time.time()
    with _lock:
        st = _states.get(host)
        if st is None:
            if len(_states) >= _MAX_HOSTS:
                # LRU evict: drop the stalest host.
                stale = min(_states, key=lambda h: _states[h]['last_seen'])
                _states.pop(stale, None)
            st = _new_state(host, origin)
            _states[host] = st
            should_wake_prober = True
            logger.debug('[Netpath] Tracking host: %s', host)
        elif now - st['last_seen'] >= _HOST_TTL:
            should_wake_prober = True
        st['last_seen'] = now
    if should_wake_prober:
        _prober_wake.set()


def decide(host: str) -> 'str | None':
    """Return the pinned path for *host*: 'direct', 'proxy', or None.

    None means "no learned preference — follow the deployment default".
    Also records the *effective* path (decision or env default) so a later
    :func:`report_outcome` can be attributed correctly.
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
        eff = st['decision']
        if eff is None:
            eff = 'proxy' if _proxy_url() else 'direct'
        st['effective'] = eff
        return st['decision']


def _is_bad(path: dict) -> bool:
    return path['fails'] >= _FAIL_THRESHOLD


def _reevaluate(st: dict) -> None:
    """Recompute st['decision'] from current path stats. Caller holds lock."""
    paths = st['paths']
    d, p = paths['direct'], paths['proxy']
    have_proxy = _proxy_url() is not None
    cur = st['decision']
    new = cur

    if _is_bad(d) and (not have_proxy or _is_bad(p)):
        # Both paths bad → stop pinning anything; env default is the last hope.
        new = None
    elif cur is not None and _is_bad(paths[cur]):
        other = 'proxy' if cur == 'direct' else 'direct'
        if other == 'proxy' and not have_proxy:
            new = None
        elif not _is_bad(paths[other]):
            new = other
        else:
            new = None
    else:
        # Latency contest among healthy, measured paths.
        candidates = []
        if d['ewma_ms'] is not None and not _is_bad(d):
            candidates.append('direct')
        if have_proxy and p['ewma_ms'] is not None and not _is_bad(p):
            candidates.append('proxy')
        if candidates:
            best = min(candidates, key=lambda k: paths[k]['ewma_ms'])
            if cur is None:
                new = best
            elif best != cur and paths[best]['samples'] >= _MIN_SAMPLES:
                cur_lat = paths[cur]['ewma_ms']
                if cur_lat is None or paths[best]['ewma_ms'] < cur_lat * _LAT_MARGIN:
                    new = best

    if new != cur:
        st['decision'] = new
        st['decision_since'] = time.time()
        logger.info('[Netpath] %s: path %s → %s (direct=%s proxy=%s)',
                    st['host'], cur or 'default', new or 'default',
                    _fmt_path(d), _fmt_path(p))


def _fmt_path(path: dict) -> str:
    if path['ewma_ms'] is None and not path['fails']:
        return 'unmeasured'
    lat = '%.0fms' % path['ewma_ms'] if path['ewma_ms'] is not None else '?'
    return '%s%s' % (lat, ' BAD' if _is_bad(path) else '')


# ═════════════════════════════════════════════════════════════
#  Outcome reporting (passive feed + prober feed)
# ═════════════════════════════════════════════════════════════

def report_outcome(url: str, ok: bool, latency_ms: 'float | None' = None,
                   *, path: 'str | None' = None) -> None:
    """Attribute a real (or probe) request outcome to a path.

    ``path`` forces attribution ('direct'/'proxy') for callers that know the
    selected route. Other traffic is attributed to the effective path the
    request actually used.
    Never raises — transports call this on their hot path.
    """
    _record_outcome(url, ok, latency_ms, path=path, probe_interval=None)


def _stable_probe_delay(period: float) -> float:
    return max(_bounded_probe_interval(period), _PROBE_MAX_INTERVAL)


def _failed_probe_delay(period: float, consecutive_failures: int) -> float:
    base = _bounded_probe_interval(period)
    exponent = max(0, min(int(consecutive_failures) - 1,
                          _MAX_PROBE_FAILURES - 1))
    return min(_stable_probe_delay(base), base * (2 ** exponent))


def _record_outcome(
    url: str,
    ok: bool,
    latency_ms: 'float | None',
    *,
    path: 'str | None',
    probe_interval: 'float | None',
) -> None:
    """Record one observation and update its bounded active-probe deadline."""
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
        path_name = path if path in ('direct', 'proxy') else st.get('effective')
        if path_name not in ('direct', 'proxy'):
            path_name = 'proxy' if _proxy_url() else 'direct'
        path_state = st['paths'][path_name]
        if ok:
            path_state['fails'] = 0
            path_state['last_ok'] = now
            if latency_ms is not None and 0 < latency_ms <= _MAX_LAT_MS:
                path_state['samples'] += 1
                if path_state['ewma_ms'] is None:
                    path_state['ewma_ms'] = float(latency_ms)
                else:
                    a = _EWMA_ALPHA
                    path_state['ewma_ms'] = (
                        a * latency_ms + (1 - a) * path_state['ewma_ms'])
        else:
            path_state['fails'] += 1
            path_state['last_fail'] = now

        wake_prober = False
        if probe_interval is not None:
            path_state['last_probe'] = now
            if ok:
                path_state['probe_failures'] = 0
                path_state['next_probe'] = (
                    now + _stable_probe_delay(probe_interval))
            else:
                probe_failures = min(
                    _MAX_PROBE_FAILURES,
                    int(path_state.get('probe_failures') or 0) + 1,
                )
                path_state['probe_failures'] = probe_failures
                path_state['next_probe'] = (
                    now + _failed_probe_delay(
                        probe_interval, probe_failures))
        elif ok:
            # Real traffic is fresher and more representative than a synthetic
            # GET. Do not probe the same healthy path again for the stable
            # interval while user requests keep validating it.
            path_state['probe_failures'] = 0
            path_state['next_probe'] = max(
                float(path_state.get('next_probe') or 0.0),
                now + _stable_probe_delay(_PROBE_INTERVAL),
            )
        else:
            # A user-visible path just failed. Recheck the alternate path now,
            # while postponing a retry of the failed path by the base cadence.
            failed_retry_at = now + _PROBE_INTERVAL
            current_due = float(path_state.get('next_probe') or 0.0)
            if current_due <= now or failed_retry_at < current_due:
                path_state['next_probe'] = failed_retry_at
            other_name = 'proxy' if path_name == 'direct' else 'direct'
            if other_name == 'direct' or _proxy_url() is not None:
                st['paths'][other_name]['next_probe'] = now
                wake_prober = True
        _reevaluate(st)
        _dirty = True
        _generation += 1
    _maybe_save()
    if wake_prober:
        _prober_wake.set()


# ═════════════════════════════════════════════════════════════
#  Active probing
# ═════════════════════════════════════════════════════════════

def _probe_once(url: str, use_proxy: bool) -> 'tuple[bool, float | None]':
    """Fetch *url* headers-only over one path. Any HTTP status = path works."""
    import requests  # local import: keep module import light for non-server use
    if use_proxy:
        proxy = _proxy_url()
        if not proxy:
            return (False, None)
        proxies = {'http': proxy, 'https': proxy}
    else:
        proxies = {'no_proxy': '*'}
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


def _probe_path(host: str, path_name: str, period: float) -> None:
    host = (host or '').lower()
    with _lock:
        st = _states.get(host)
        url = st['sample_url'] if st else None
    if not url:
        return
    use_proxy = path_name == 'proxy'
    if use_proxy and not _proxy_url():
        return
    ok, latency_ms = _probe_once(url, use_proxy)
    _record_outcome(
        url,
        ok,
        latency_ms,
        path=path_name,
        probe_interval=period,
    )


def probe_host(host: str) -> None:
    """Probe both paths for one host and feed the scorer. Exposed for tests."""
    for path_name in ('direct', 'proxy'):
        _probe_path(host, path_name, _PROBE_INTERVAL)


def _eligible_probe_paths(now: float) -> list[tuple[str, str]]:
    have_proxy = _proxy_url() is not None
    with _lock:
        due: list[tuple[str, str]] = []
        for host, state in _states.items():
            if now - state['last_seen'] >= _HOST_TTL:
                continue
            for path_name in ('direct', 'proxy'):
                if path_name == 'proxy' and not have_proxy:
                    continue
                path_state = state['paths'][path_name]
                if float(path_state.get('next_probe') or 0.0) <= now:
                    due.append((host, path_name))
        return due


def _seconds_until_next_probe(period: float) -> float:
    now = time.time()
    have_proxy = _proxy_url() is not None
    stable_delay = _stable_probe_delay(period)
    with _lock:
        deadlines = [
            float(path_state.get('next_probe') or 0.0)
            for state in _states.values()
            if now - state['last_seen'] < _HOST_TTL
            for path_name, path_state in state['paths'].items()
            if path_name == 'direct' or have_proxy
        ]
    if not deadlines:
        return stable_delay
    return min(stable_delay, max(0.0, min(deadlines) - now))


def _defer_probe_after_internal_error(
    host: str,
    path_name: str,
    period: float,
) -> None:
    """Prevent an unexpected probe implementation fault from busy-looping."""
    global _dirty, _generation
    with _lock:
        state = _states.get(host)
        if state is None:
            return
        state['paths'][path_name]['next_probe'] = (
            time.time() + _bounded_probe_interval(period))
        _dirty = True
        _generation += 1


def _probe_round(period: 'float | None' = None) -> float:
    """Probe only due paths and return the bounded delay until more work."""
    bounded_period = _bounded_probe_interval(
        _PROBE_INTERVAL if period is None else period)
    due_paths = _eligible_probe_paths(time.time())
    previous_host = None
    for host, path_name in due_paths:
        if _prober_stop.is_set():
            return _stable_probe_delay(bounded_period)
        if previous_host is not None and host != previous_host:
            # Small jitter between hosts so a due set never bursts.
            if _prober_stop.wait(random.uniform(0.1, 0.4)):
                return _stable_probe_delay(bounded_period)
        try:
            _probe_path(host, path_name, bounded_period)
        except Exception as e:
            logger.debug(
                '[Netpath] %s probe failed for %s: %s',
                path_name, host, e)
            _defer_probe_after_internal_error(
                host, path_name, bounded_period)
        previous_host = host
    _save()
    return _seconds_until_next_probe(bounded_period)


def start_prober(interval: 'float | None' = None) -> bool:
    """Start the background probe loop (idempotent). Returns True if running."""
    global _prober_thread
    if not _enabled():
        logger.debug('[Netpath] Disabled via TOFU_NETPATH — prober not started')
        return False
    with _lock:
        if _prober_thread is not None and _prober_thread.is_alive():
            # A timed-out shutdown keeps the exact old owner attached until it
            # exits; never clear its stop signal and launch a duplicate worker.
            return not _prober_stop.is_set()
        _prober_stop.clear()
        period = _bounded_probe_interval(
            _PROBE_INTERVAL if interval is None else interval)
        _prober_wake.clear()
        _prober_thread = threading.Thread(
            target=_prober_loop, args=(period,),
            name='netpath-prober', daemon=True)
        _prober_thread.start()
    logger.info(
        '[Netpath] Prober started (base %.0fs, stable max %.0fs, timeout %.1fs)',
        period, _stable_probe_delay(period), _PROBE_TIMEOUT)
    return True


def _prober_loop(period: float) -> None:
    # First round soon after boot (paths are unknown — data is most valuable
    # early), then wait until the nearest per-path adaptive deadline.
    _prober_wake.wait(10)
    if _prober_stop.is_set():
        return
    while not _prober_stop.is_set():
        _prober_wake.clear()
        try:
            delay = _probe_round(period)
        except Exception as e:
            logger.debug('[Netpath] probe round failed: %s', e)
            delay = period
        if _prober_stop.is_set():
            return
        _prober_wake.wait(max(0.05, delay))


def stop_prober() -> None:
    """Stop the background probe loop (test helper / shutdown)."""
    global _prober_thread
    _prober_stop.set()
    _prober_wake.set()
    t = _prober_thread
    if t is not None and t.is_alive():
        t.join(timeout=max(5.0, min(15.0, (_PROBE_TIMEOUT * 2) + 1.0)))
    with _lock:
        if _prober_thread is t and (t is None or not t.is_alive()):
            _prober_thread = None
    if t is not None and t.is_alive():
        logger.warning(
            '[Netpath] Prober did not stop before the join deadline; '
            'retaining its owner and stop signal')
    else:
        _prober_wake.clear()


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


def _restored_timestamp(
    value: object,
    *,
    now: float,
    latest: 'float | None' = None,
) -> float:
    """Decode persisted wall time without allowing immortal/far-future work."""
    try:
        timestamp = float(value)
    except (ValueError, TypeError):
        return 0.0
    if not math.isfinite(timestamp) or timestamp < 0:
        return 0.0
    return min(timestamp, now if latest is None else latest)


def _load() -> None:
    global _dirty, _generation
    payload = read_json(_STORE_PATH, default=None)
    if not isinstance(payload, dict) or payload.get('version') != _STORE_VERSION:
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
                # Preserve the real last-use time. Refreshing every restored
                # host at boot made abandoned endpoints active for another 24
                # hours and caused thousands of pointless network probes.
                fresh = _new_state(host, sample_url)
                decision = st.get('decision')
                fresh['decision'] = (decision if decision in (
                    'direct', 'proxy') else None)
                fresh['last_seen'] = _restored_timestamp(
                    st.get('last_seen'), now=now)
                fresh['decision_since'] = _restored_timestamp(
                    st.get('decision_since'), now=now)
                for name in ('direct', 'proxy'):
                    src = (st.get('paths') or {}).get(name) or {}
                    if not isinstance(src, dict):
                        src = {}
                    dst = fresh['paths'][name]
                    latency = src.get('ewma_ms')
                    dst['ewma_ms'] = (float(latency) if latency is not None
                                      else None)
                    dst['samples'] = max(0, int(src.get('samples') or 0))
                    dst['fails'] = max(0, int(src.get('fails') or 0))
                    dst['last_ok'] = _restored_timestamp(
                        src.get('last_ok'), now=now)
                    dst['last_fail'] = _restored_timestamp(
                        src.get('last_fail'), now=now)
                    dst['last_probe'] = _restored_timestamp(
                        src.get('last_probe'), now=now)
                    dst['probe_failures'] = min(
                        _MAX_PROBE_FAILURES,
                        max(0, int(src.get(
                            'probe_failures', dst['fails']) or 0)),
                    )
                    dst['next_probe'] = _restored_timestamp(
                        src.get('next_probe'),
                        now=now,
                        latest=now + _HARD_MAX_PROBE_INTERVAL,
                    )
                _states[host] = fresh
                restored += 1
            except (TypeError, ValueError, OverflowError) as error:
                logger.debug('[Netpath] skipped malformed host state: %s',
                             error)
        _dirty = False
        _generation = 0
    logger.info('[Netpath] Restored %d host(s) from disk', restored)


def status_summary() -> dict:
    """Compact per-host view for the Settings UI / diagnostics."""
    with _lock:
        out = {}
        for host, st in _states.items():
            out[host] = {
                'decision': st['decision'] or 'default',
                'direct_ms': _round(st['paths']['direct']['ewma_ms']),
                'proxy_ms': _round(st['paths']['proxy']['ewma_ms']),
                'direct_fails': st['paths']['direct']['fails'],
                'proxy_fails': st['paths']['proxy']['fails'],
            }
        return {'enabled': _enabled(), 'hosts': out}


def _round(v):
    return None if v is None else round(float(v), 1)


def reset_proxy_stats() -> None:
    """Invalidate all proxy-path measurements (proxy address changed)."""
    global _dirty, _generation
    changed = False
    with _lock:
        for st in _states.values():
            old = st['paths']['proxy']
            if (old['ewma_ms'] is not None or old['samples'] or old['fails']
                    or st['decision'] == 'proxy'):
                changed = True
            st['paths']['proxy'] = _new_path()
            if st['decision'] == 'proxy':
                st['decision'] = None
        if changed:
            _dirty = True
            _generation += 1
    if changed:
        _save()
    # A newly configured proxy can make a previously ineligible path due even
    # when there were no old proxy measurements to clear.
    _prober_wake.set()
    logger.info('[Netpath] Proxy stats reset (proxy address changed)')


def reset_for_test() -> None:
    """Clear all learned state and stop the prober. Test-only."""
    global _dirty, _generation, _last_save
    stop_prober()
    with _lock:
        _states.clear()
        _dirty = False
        _generation = 0
        _last_save = 0.0


_load()
