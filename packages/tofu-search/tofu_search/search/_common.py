"""lib/search/_common.py — Shared constants and helpers for search engines.

Exposes:
  HEADERS          — standard User-Agent / Accept-Encoding headers
  clean_text       — HTML-strip + entity-decode + control-char cleanup
  http_search_get  — the shared "timed requests.get + error-handling + elapsed
                     logging" skeleton used by every engine. Accepts a
                     *parser* callable that converts the successful response
                     into a list of result dicts.

Engine modules under ``tofu_search/search/engines/`` use ``http_search_get`` so they
only own their parser and URL quirks — the HTTP envelope is DRY.
"""

import random
import re
import threading
import time
import unicodedata
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor, as_completed
from html import unescape

import requests
from bs4 import BeautifulSoup
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from tofu_search.config import get_config
from tofu_search.log import get_logger
from tofu_search.search.proxy_mode import proxy_mode_manager

logger = get_logger(__name__)

__all__ = [
    'HEADERS', 'clean_text', 'http_search_get',
    'soup_of', 'make_result', 'search_session', 'engine_circuit',
    'host_throttle',
]

# A 200 response whose body is at least this large but parses to ZERO result
# blocks is treated as a soft block (consent wall / bot interstitial / locale
# redirect served 200), NOT a genuine "no matches" — worth retrying the other
# network path when a proxy is available. Matches the per-engine parse-health
# threshold used by the Bing/Brave parsers.
_SOFT_BLOCK_BODY_BYTES = 20_000


def _is_connection_failure(exc: Exception) -> bool:
    """True for connect/proxy/DNS-level failures worth retrying the OTHER path.

    A read-timeout (the endpoint accepted the connection but couldn't answer in
    time) is deliberately EXCLUDED — switching network path won't make a slow
    endpoint fast, and a full second attempt would blow the time budget.
    """
    if isinstance(exc, requests.exceptions.ProxyError):
        return True
    if isinstance(exc, requests.exceptions.ConnectTimeout):
        return True
    if isinstance(exc, requests.exceptions.ReadTimeout):
        return False
    # ConnectionError covers DNS failure, connection refused/reset — but
    # ConnectTimeout (already handled) also subclasses it, so this is the
    # residual "couldn't establish the connection" bucket.
    if isinstance(exc, requests.exceptions.ConnectionError):
        return True
    return False


# Blocking HTTP statuses that justify retrying the other network path: a proxy
# auth demand (407), an egress-IP block (403), rate-limit (429), or a 5xx that
# is often an interstitial served by a blocking edge.
_RETRYABLE_STATUSES = frozenset({403, 407, 429, 500, 502, 503, 504})

HEADERS = {
    'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) '
                  'AppleWebKit/537.36 (KHTML, like Gecko) '
                  'Chrome/121.0.0.0 Safari/537.36',
    'Accept-Encoding': 'gzip, deflate',      # avoid brotli decode issues
    'Accept-Language': 'en-US,en;q=0.9',
}


# ═══════════════════════════════════════════════════════
#  Shared HTTP session — connection pooling + retry
# ═══════════════════════════════════════════════════════
# A single Session reused by every engine amortises the TCP/TLS handshake
# across the 6 engines (and their retries) instead of opening a fresh
# connection per requests.get(). Retry covers transient connect failures and
# the rate-limit / 5xx status codes; read-timeout retries are disabled (a
# search endpoint that can't answer within the timeout won't on a retry).
_retry = Retry(
    total=2,
    connect=2,
    read=0,
    backoff_factor=0.5,
    status_forcelist=[429, 500, 502, 503, 504],
    allowed_methods=['GET'],
    raise_on_status=False,
)
search_session = requests.Session()
search_session.headers.update(HEADERS)
_adapter = HTTPAdapter(pool_connections=16, pool_maxsize=32, max_retries=_retry)
search_session.mount('https://', _adapter)
search_session.mount('http://', _adapter)


# ═══════════════════════════════════════════════════════
#  Per-engine circuit breaker
# ═══════════════════════════════════════════════════════

class _EngineCircuit:
    """Skip an engine for a cooldown after consecutive failures.

    Keyed by engine name (``'Bing'``, ``'Brave'`` …). A run of
    ``FAIL_THRESHOLD`` failures (timeout / network error / non-2xx) trips the
    breaker; the engine is skipped for ``COOLDOWN`` seconds, then given another
    chance. Any success resets the counter. This stops a hard-down or
    IP-blocking engine from costing every query its full timeout budget.
    """
    FAIL_THRESHOLD = 3
    COOLDOWN = 120

    def __init__(self):
        self._lock = threading.Lock()
        self._state: dict[str, dict] = {}  # name -> {fails, tripped_at}

    def is_open(self, name: str) -> bool:
        with self._lock:
            st = self._state.get(name)
            if not st or st['tripped_at'] is None:
                return False
            if time.time() - st['tripped_at'] > self.COOLDOWN:
                del self._state[name]
                return False
            return True

    def record_failure(self, name: str):
        with self._lock:
            st = self._state.setdefault(name, {'fails': 0, 'tripped_at': None})
            st['fails'] += 1
            if st['fails'] >= self.FAIL_THRESHOLD and st['tripped_at'] is None:
                st['tripped_at'] = time.time()
                logger.warning('[Search] Circuit OPEN for engine %s — %d consecutive '
                               'failures, cooling down %ds', name, st['fails'], self.COOLDOWN)

    def record_success(self, name: str):
        with self._lock:
            self._state.pop(name, None)


engine_circuit = _EngineCircuit()


# ═══════════════════════════════════════════════════════
#  Per-engine request throttle (self-inflicted rate-limit guard)
# ═══════════════════════════════════════════════════════

class _HostThrottle:
    """Space out requests to the SAME engine to a minimum interval.

    Process-global and keyed by engine name (``'DDG-HTML'``, ``'Bing'`` …),
    exactly like :class:`_EngineCircuit`. The bug it fixes is two CONCURRENT
    ``perform_web_search`` calls (e.g. two parallel recommend batches) hitting
    one engine within the same second and tripping its rate-limit (the observed
    DDG-HTML ``202``). Because the state is a module global both calls consult,
    the second caller's request is delayed until the interval has elapsed.

    Per-engine locking: each engine has its OWN lock, so a wait on a busy
    engine never serializes a request to a DIFFERENT engine — the engine +
    fetch overlap the orchestrator relies on is preserved.

    Jitter is upward-only ([0, +JITTER_FRAC] of the interval): the realized
    spacing is always >= the configured interval, while two threads that would
    otherwise re-collide on the next tick desynchronize.
    """
    JITTER_FRAC = 0.30

    def __init__(self):
        self._guard = threading.Lock()        # guards _locks / _last mutation
        self._locks: dict[str, threading.Lock] = {}
        self._last: dict[str, float] = {}     # engine -> last-request monotonic ts

    def _lock_for(self, name: str) -> threading.Lock:
        with self._guard:
            lk = self._locks.get(name)
            if lk is None:
                lk = self._locks[name] = threading.Lock()
            return lk

    def _interval(self) -> float:
        try:
            return max(0.0, get_config().min_request_interval_ms / 1000.0)
        except Exception:
            # Fail-open: a config error must never stall search.
            return 0.0

    def wait(self, name: str, *, max_wait: float | None = None) -> float:
        """Block until at least ``interval`` has elapsed since this engine's last
        request, then stamp the new request time. Returns the seconds actually
        slept (0.0 when the throttle is disabled or the interval already passed).

        ``max_wait`` clamps the sleep to the caller's remaining budget (the
        per-request timeout), so the throttle never pushes a query past its
        deadline.
        """
        interval = self._interval()
        if interval <= 0:
            return 0.0
        lk = self._lock_for(name)
        with lk:
            now = time.monotonic()
            last = self._last.get(name)
            slept = 0.0
            if last is not None:
                gap = now - last
                if gap < interval:
                    jitter = random.uniform(0.0, interval * self.JITTER_FRAC)
                    delay = (interval - gap) + jitter
                    if max_wait is not None:
                        delay = min(delay, max_wait)
                    if delay > 0:
                        time.sleep(delay)
                        slept = delay
            self._last[name] = time.monotonic()
            return slept

    def reset(self):
        """Drop all per-engine state (test isolation — this global is NOT reset
        by the shared conftest, mirroring engine_circuit)."""
        with self._guard:
            self._locks.clear()
            self._last.clear()


host_throttle = _HostThrottle()

# ═══════════════════════════════════════════════════════
#  Network-path race pool (parallel proxy failover)
# ═══════════════════════════════════════════════════════
# Lazy process-wide pool, used ONLY when the first planned network path failed
# and >=2 alternates remain (multi-proxy failover chain + direct). Racing
# bounds a dead/wedged primary proxy at ONE per-request timeout instead of
# paying it serially before the next path is even tried. Threads are short-
# lived and timeout-bounded by the request itself; losers are abandoned in
# flight and never touch the per-engine proxy learning.
_race_pool = None
_race_pool_lock = threading.Lock()


def _race_executor() -> ThreadPoolExecutor:
    global _race_pool
    with _race_pool_lock:
        if _race_pool is None:
            _race_pool = ThreadPoolExecutor(
                max_workers=6, thread_name_prefix='tofu-proxy-race')
        return _race_pool


# ═══════════════════════════════════════════════════════
#  HTML parsing helpers
# ═══════════════════════════════════════════════════════

def soup_of(html: str) -> BeautifulSoup:
    """Parse HTML with the stdlib ``html.parser``.

    NOTE: ``html.parser`` is used deliberately, NOT ``lxml``. lxml/libxml2 is
    thread-unsafe under the orchestrator's concurrent worker pools and has
    been observed to segfault (see fetch/html_extract.py). html.parser is
    pure-Python and GIL-safe.
    """
    return BeautifulSoup(html, 'html.parser')


def make_result(title: str, snippet: str, url: str, source: str,
                *, title_max: int = 200, snippet_max: int = 500) -> dict:
    """Build a cleaned, length-capped search-result dict."""
    return {
        'title': clean_text(title)[:title_max],
        'snippet': clean_text(snippet)[:snippet_max],
        'url': url,
        'source': source,
    }


def clean_text(s):
    """Clean a search result string: strip HTML, decode entities, remove junk chars."""
    if not s:
        return ''
    s = re.sub(r'<[^>]+>', ' ', s)
    s = unescape(s)
    s = re.sub(r'[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]', '', s)
    s = re.sub(r'[\u200b\u200c\u200d\u200e\u200f\ufeff\u00ad]', '', s)
    s = unicodedata.normalize('NFC', s)
    s = re.sub(r'\s+', ' ', s).strip()
    return s


def http_search_get(
    *,
    name: str,
    url: str,
    params: dict,
    query: str,
    parser: Callable[[requests.Response], list],
    max_results: int = 6,
    timeout: int = 12,
    headers: dict | None = None,
    on_ratelimit_retry: bool = False,
) -> list:
    """Shared HTTP envelope for scraping search engines.

    Parameters
    ----------
    name : str
        Engine name (``'Bing'``, ``'Brave'``, ``'DDG-HTML'``…) — used in log
        prefixes only.
    url : str
        Full endpoint URL.
    params : dict
        Query-string parameters passed to ``requests.get``.
    query : str
        Original user query — logged for diagnostics only.
    parser : callable
        ``parser(response) -> list[dict]``. Only invoked for a successful
        (``resp.ok``) response. Parser owns all format-specific regex / HTML
        handling.
    max_results : int
        Cap on number of results — enforced after parse, trimming overflow.
    timeout : int
        Per-request timeout in seconds.
    headers : dict, optional
        Override headers. Defaults to module-level ``HEADERS``.
    on_ratelimit_retry : bool
        If ``True`` and HTTP 202 is returned, sleep 0.6 s and retry once
        (DDG rate-limit behavior). Other engines set it to ``False``.

    Returns
    -------
    list
        Up to ``max_results`` parsed result dicts. Empty list ONLY for a
        genuine no-match (a successful HTTP round-trip that parsed to zero
        results) or a circuit-breaker skip.

    Raises
    ------
    requests.RequestException
        When every attempt failed at the transport level — the LAST
        exception is re-raised. The orchestrator classifies a raised engine
        as ``engine_errors`` (network failure), NOT ``engine_empty``
        (no matches); returning ``[]`` here used to disguise a total
        network outage as "no matches", which the model and the UI both
        reported as fact.
    requests.exceptions.HTTPError
        When the last attempt was answered with a non-OK HTTP status.
    """
    tag = f'[Search] {name}'

    # ── Circuit breaker: skip an engine that has been failing repeatedly ──
    # (A benched engine returns here BEFORE the throttle, so it spends zero
    #  interval budget.)
    if engine_circuit.is_open(name):
        logger.info('%s skipped (circuit open) query=%r', tag, query[:60])
        return []

    # ── Per-engine request throttle: two concurrent search calls hitting this
    #    same engine within the interval serialize to >= it (self-inflicted
    #    rate-limit guard). Clamped to the request timeout so the wait spends
    #    budget the caller already has, never pushing past its deadline. Only
    #    the HTML-engine envelope is throttled — the JSON vertical path uses a
    #    separate http_get and stays unthrottled. ──
    host_throttle.wait(name, max_wait=float(timeout))

    hdrs = headers or HEADERS

    def _get(proxies_kwarg):
        kw = {'params': params, 'headers': hdrs, 'timeout': timeout}
        if proxies_kwarg is not None:
            kw['proxies'] = proxies_kwarg
        return search_session.get(url, **kw)

    cfg = get_config()
    # ── Adaptive proxy plan: one attempt when no proxy is configured (identical
    #    to the historical env-only path), else the failover chain (proxy chain
    #    → direct) in sticky-learned order. See search/proxy_mode.py. ──
    plan = proxy_mode_manager.attempt_plan(name, cfg)

    t0 = time.time()
    results: list = []
    failed = True   # until an attempt genuinely succeeds
    last_exc: Exception | None = None
    last_http_status: int | None = None

    def _attempt(mode, proxies_kwarg):
        """One network-path attempt → a fine-grained outcome tuple.

        ('results', list) — HTTP ok, parser found results (genuine success)
        ('empty',  None)  — HTTP ok, SMALL body, zero results (genuine no-match)
        ('soft',   int)   — HTTP ok, BIG body, zero results (consent wall / bot
                            interstitial served 200 — suspicious, worth another
                            path when one remains)
        ('status', int)   — non-OK HTTP status
        ('conn',   exc)   — connect-level failure (proxy/DNS/refused): another
                            path may genuinely help
        ('fatal',  exc)   — read-timeout / unexpected: switching paths won't
                            make a slow endpoint fast
        """
        try:
            resp = _get(proxies_kwarg)

            # Rate-limit retry (DDG-specific, opt-in) — same network path.
            if on_ratelimit_retry and resp.status_code == 202:
                logger.info('%s 202 (rate-limited), retry in 0.6s: %s', tag, query[:80])
                time.sleep(0.6)
                resp = _get(proxies_kwarg)

            if not resp.ok:
                return ('status', resp.status_code)

            parsed = parser(resp) or []
            if len(parsed) > max_results:
                parsed = parsed[:max_results]
            if not parsed:
                body_len = len(getattr(resp, 'text', '') or '')
                if body_len > _SOFT_BLOCK_BODY_BYTES:
                    return ('soft', body_len)
                return ('empty', None)
            return ('results', parsed)
        except requests.RequestException as e:
            return ('conn', e) if _is_connection_failure(e) else ('fatal', e)
        except Exception as e:
            logger.error('%s error via %s: %s', tag, mode, e, exc_info=True)
            return ('fatal', e)

    def _race(attempts):
        """Race the remaining network paths CONCURRENTLY; first genuine
        success wins. Returns ('ok', results) / ('fatal', exc) /
        ('status', code-or-None). Losers still in flight are abandoned
        (cancelled when not yet started) and never touch the per-engine
        learning; completed losers report their failure so a pinned-but-dead
        path gets unpinned."""
        logger.info('%s racing %d alternate network paths in parallel: %s',
                    tag, len(attempts), [m for m, _ in attempts])
        pool = _race_executor()
        futures = {pool.submit(_attempt, m, kw): m for m, kw in attempts}
        fatal_exc = conn_exc = None
        status = None
        for fut in as_completed(futures):
            rmode = futures[fut]
            try:
                kind, payload = fut.result()
            except Exception as e:      # _attempt never raises; belt-and-braces
                logger.error('%s race attempt via %s raised: %s', tag, rmode, e,
                             exc_info=True)
                if fatal_exc is None:
                    fatal_exc = e
                continue
            if kind in ('results', 'empty'):
                for other in futures:
                    if other is not fut:
                        other.cancel()
                proxy_mode_manager.record_success(name, rmode)
                logger.info('%s race won by %s', tag, rmode)
                return ('ok', payload or [])
            if kind == 'conn':
                if conn_exc is None:
                    conn_exc = payload
                proxy_mode_manager.record_failure(name, rmode)
            elif kind == 'fatal':
                if fatal_exc is None:
                    fatal_exc = payload
            elif kind == 'status':
                if payload in _RETRYABLE_STATUSES:
                    proxy_mode_manager.record_failure(name, rmode)
                if status is None:
                    status = payload
            else:   # soft block
                proxy_mode_manager.record_failure(name, rmode)
        logger.info('%s race: all %d alternate paths failed', tag, len(attempts))
        if fatal_exc is not None or conn_exc is not None:
            return ('fatal', fatal_exc if fatal_exc is not None else conn_exc)
        return ('status', status)

    idx = 0
    while idx < len(plan):
        mode, proxies_kwarg = plan[idx]
        remaining = plan[idx + 1:]
        kind, payload = _attempt(mode, proxies_kwarg)

        if kind in ('results', 'empty'):
            # Genuine success (a small-body 0-result page is a real no-match).
            results = payload or []
            failed = False
            proxy_mode_manager.record_success(name, mode)
            break

        retryable = (kind in ('conn', 'soft')
                     or (kind == 'status' and payload in _RETRYABLE_STATUSES))
        if retryable and remaining:
            if kind == 'conn':
                logger.info('%s connect failure via %s (%s) — trying alternate network path',
                            tag, mode, type(payload).__name__)
            elif kind == 'soft':
                logger.info('%s 200 but parsed 0 results (%d bytes) via %s — '
                            'likely soft block, trying alternate network path',
                            tag, payload, mode)
            else:
                logger.info('%s HTTP %d via %s — trying alternate network path',
                            tag, payload, mode)
            proxy_mode_manager.record_failure(name, mode)
            # With TWO OR MORE alternates left, race them in parallel: a wedged
            # primary proxy would otherwise cost a full timeout per path before
            # the next one is even tried (racing bounds failover at 1× timeout).
            if len(remaining) >= 2 and getattr(cfg, 'proxy_race', True):
                rkind, rpayload = _race(remaining)
                if rkind == 'ok':
                    results = rpayload
                    failed = False
                elif rkind == 'status':
                    last_http_status = rpayload
                else:
                    last_exc = rpayload
                break
            idx += 1
            continue

        if kind == 'soft':
            # No alternate path left: count the big-body zero-result page as a
            # genuine no-match rather than an outage (historical behaviour).
            failed = False
            proxy_mode_manager.record_success(name, mode)
            break
        if kind == 'status':
            logger.warning('%s returned HTTP %d via %s for query: %s',
                           tag, payload, mode, query[:80])
            last_http_status = payload
            break
        # conn on the last path, or fatal anywhere (read-timeout / unexpected —
        # the unexpected case was already error-logged inside _attempt).
        if isinstance(payload, requests.Timeout):
            logger.warning('%s timeout via %s for query: %s', tag, mode, query[:80])
        elif isinstance(payload, requests.RequestException):
            logger.warning('%s request failed via %s for query %r: %s: %s', tag, mode, query[:80], type(payload).__name__, payload)
        last_exc = payload
        break

    if failed:
        engine_circuit.record_failure(name)
        # A total failure is an ERROR, not an empty result set — raise so
        # the orchestrator files it under engine_errors (network outage)
        # instead of engine_empty (no matches).
        if last_exc is not None:
            raise last_exc
        raise requests.exceptions.HTTPError(
            f'{name} returned HTTP {last_http_status}'
            if last_http_status is not None else f'{name} request failed')
    else:
        # A successful HTTP round-trip resets the breaker even when the parse
        # yields 0 results — "no matches" is not an engine fault.
        engine_circuit.record_success(name)

    elapsed = time.time() - t0
    logger.info('%s: %d results in %.1fs  query=%r', tag, len(results), elapsed, query[:60])
    return results
