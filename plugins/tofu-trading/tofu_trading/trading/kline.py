"""tofu_trading/trading/kline.py — multi-source K-line fetch with health-based failover.

Why this module exists
----------------------
The old code hardcoded EastMoney ``push2his`` in three separate files
(``historical_data.py``, ``nav.py``, ``market.py``) with no fallback. Measured
from this deployment (behind a corporate proxy) on 2026-07-26::

    push2his x4 attempts  → 1 hard RemoteDisconnected, 3 x rc:102 (no data)
    push2 (non-his)       → HTTP 502
    web.ifzq.gtimg.cn    → 200, real forward-adjusted OHLC

So EastMoney is not a reliable primary *here*. But note the inversion risk: on
a different network the ranking may flip back. Therefore the primary is NOT
hardcoded — sources are ordered by a **runtime health probe** whose result is
cached, and a source that fails mid-request falls through to the next one.

Contract
--------
``fetch_kline(code, start_date, end_date)`` returns a list of dicts sorted
chronologically::

    [{'date': 'YYYY-MM-DD', 'nav': close, 'acc_nav': close, 'change_pct': pct}, ...]

``nav``/``acc_nav`` duplicate the close price to stay wire-compatible with the
fund NAV shape the rest of the codebase already consumes.

An empty list means "no source could answer" — callers must treat that as
missing data, never as zero.
"""

import json
import re
import threading
import time

from lib.log import get_logger

from ._common import stock_secid

logger = get_logger(__name__)

__all__ = [
    'fetch_kline',
    'probe_sources',
    'get_source_health',
    'KLINE_SOURCES',
]

# Health probe results live here: {source_name: {'ok': bool, 'checked_at': ts,
# 'latency_ms': float, 'error': str}}
_health: dict = {}
_health_lock = threading.Lock()

# Re-probe at most this often. A source that just failed a real request is
# marked stale immediately, so this only bounds *proactive* probing.
_PROBE_TTL = 600.0

# Transient transport failures (proxy resets) are retried this many times per
# source before falling through. Measured: EastMoney behind this deployment's
# proxy fails roughly 1 in 4 attempts with RemoteDisconnected.
_MAX_ATTEMPTS = 3
_RETRY_BACKOFF = 0.4

# A well-known liquid symbol used purely as a probe target (工商银行).
_PROBE_CODE = '601398'


# ═══════════════════════════════════════════════════════════
#  Per-source parsers
# ═══════════════════════════════════════════════════════════

def _fetch_tencent(client, code, sd, ed):
    """Tencent ``web.ifzq.gtimg.cn`` — forward-adjusted (前复权) daily bars.

    Takes ISO ``YYYY-MM-DD`` dates. Measured: this endpoint accepts dashed
    dates and returns 0 bars for compact ``YYYYMMDD`` — an empty-but-HTTP-200
    response, i.e. a silent failure. Do not "normalise" these dates.
    """
    market = 'sh' if code[0] in ('5', '6', '9') else 'sz'
    url = (
        f'https://web.ifzq.gtimg.cn/appstock/app/fqkline/get'
        f'?param={market}{code},day,{sd or ""},{ed or ""},640,qfq'
    )
    r = client.session.get(url, timeout=10)
    payload = r.json()
    node = (payload.get('data') or {}).get(f'{market}{code}') or {}
    # Tencent returns the adjusted series under 'qfqday', falling back to 'day'.
    bars = node.get('qfqday') or node.get('day') or []
    out = []
    prev_close = None
    for row in bars:
        if len(row) < 4:
            continue
        try:
            date, close = row[0], float(row[2])
        except (ValueError, TypeError):
            continue
        # Tencent does not ship a change% column; derive it so the output shape
        # matches EastMoney's rather than silently reporting 0.
        pct = 0.0 if prev_close in (None, 0) else round(
            (close - prev_close) / prev_close * 100, 4)
        out.append({'date': date, 'nav': close, 'acc_nav': close,
                    'change_pct': pct})
        prev_close = close
    return out


def _fetch_eastmoney(client, code, sd, ed):
    """EastMoney ``push2his`` — kept as a fallback, not the primary.

    Takes ISO ``YYYY-MM-DD`` dates and converts to the compact ``YYYYMMDD``
    this endpoint requires. Each source owns its own date formatting — a
    shared "normalised" format silently broke Tencent.
    """
    beg = sd.replace('-', '') if sd else '19900101'
    end = ed.replace('-', '') if ed else '20991231'
    url = (
        f'https://push2his.eastmoney.com/api/qt/stock/kline/get?'
        f'secid={stock_secid(code)}&ut=fa5fd1943c7b386f172d6893dbfba10b'
        f'&fields1=f1,f2,f3,f4,f5,f6'
        f'&fields2=f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61'
        f'&klt=101&fqt=1&beg={beg}&end={end}&lmt=5000'
    )
    r = client.session.get(url, timeout=10, headers={
        **client.headers, 'Referer': 'https://quote.eastmoney.com/'})
    text = r.text
    m = re.search(r'jQuery\((.*)\)', text, re.S)
    payload = json.loads(m.group(1)) if m else r.json()
    klines = ((payload.get('data') or {}) or {}).get('klines') or []
    out = []
    for line in klines:
        parts = line.split(',')
        if len(parts) < 7:
            continue
        try:
            close = float(parts[2])
            pct = float(parts[8]) if len(parts) > 8 else 0.0
        except (ValueError, IndexError):
            continue
        out.append({'date': parts[0], 'nav': close, 'acc_nav': close,
                    'change_pct': pct})
    return out


# Ordered by *measured* reliability on this deployment; probe_sources() may
# reorder at runtime. Declaring it as data keeps the source list testable.
KLINE_SOURCES = (
    ('tencent', _fetch_tencent),
    ('eastmoney', _fetch_eastmoney),
)


# ═══════════════════════════════════════════════════════════
#  Health probing + ordering
# ═══════════════════════════════════════════════════════════

def _mark(name, ok, latency_ms=0.0, error=''):
    with _health_lock:
        _health[name] = {'ok': bool(ok), 'checked_at': time.time(),
                         'latency_ms': round(latency_ms, 1), 'error': str(error)[:200]}


def get_source_health() -> dict:
    """Return a copy of the current per-source health map (for /health endpoints)."""
    with _health_lock:
        return {k: dict(v) for k, v in _health.items()}


def probe_sources(*, client=None, force=False) -> dict:
    """Probe every source with a tiny real request and record health.

    Args:
        client: Optional ``TradingClient`` for dependency injection.
        force:  Probe even if a cached result is still fresh.

    Returns:
        The health map, same shape as :func:`get_source_health`.
    """
    if client is None:
        from ._common import _get_default_client
        client = _get_default_client()

    now = time.time()
    for name, fn in KLINE_SOURCES:
        with _health_lock:
            cached = _health.get(name)
        if not force and cached and (now - cached['checked_at']) < _PROBE_TTL:
            continue
        t0 = time.time()
        try:
            rows = fn(client, _PROBE_CODE, None, None)
            latency = (time.time() - t0) * 1000
            if rows:
                _mark(name, True, latency)
                logger.info('[KLine] source=%s healthy (%d bars, %.0fms)',
                            name, len(rows), latency)
            else:
                # Reachable but returning nothing is a FAILURE for our purposes
                # — this is exactly EastMoney's rc:102 shape, which would
                # otherwise look like "this symbol has no history".
                _mark(name, False, latency, 'returned 0 bars')
                logger.warning('[KLine] source=%s reachable but returned 0 bars', name)
        except Exception as e:
            _mark(name, False, (time.time() - t0) * 1000, e)
            logger.warning('[KLine] source=%s probe failed: %s', name, e)
    return get_source_health()


def _ordered_sources():
    """Sources ordered healthy-first, preserving declared order within a tier."""
    with _health_lock:
        snapshot = {k: dict(v) for k, v in _health.items()}

    def rank(item):
        name, _ = item
        h = snapshot.get(name)
        if h is None:
            return 1          # unprobed — try before known-bad
        return 0 if h['ok'] else 2

    return sorted(KLINE_SOURCES, key=rank)


# ═══════════════════════════════════════════════════════════
#  Public fetch
# ═══════════════════════════════════════════════════════════

def fetch_kline(code, start_date=None, end_date=None, *, client=None):
    """Fetch daily K-line history, trying each source until one answers.

    Args:
        code:       Stock/ETF code, e.g. ``'600519'``.
        start_date: ``'YYYY-MM-DD'`` or None for all available history.
        end_date:   ``'YYYY-MM-DD'`` or None for today.
        client:     Optional ``TradingClient`` for dependency injection.

    Returns:
        Chronologically sorted list of bar dicts; ``[]`` if every source failed.
    """
    if client is None:
        from ._common import _get_default_client
        client = _get_default_client()

    sd = start_date or ''
    ed = end_date or ''

    errors = []
    for name, fn in _ordered_sources():
        t0 = time.time()
        rows, err = None, None
        # Transport flakiness (proxy RemoteDisconnected) is common on this
        # deployment and is NOT the same as "this source is broken". Retry a
        # couple of times before demoting the source and moving on.
        for attempt in range(_MAX_ATTEMPTS):
            try:
                rows = fn(client, code, sd, ed)
                err = None
                break
            except Exception as e:
                err = e
                if attempt + 1 < _MAX_ATTEMPTS:
                    time.sleep(_RETRY_BACKOFF * (attempt + 1))
                    logger.debug('[KLine] %s attempt %d/%d failed for %s: %s',
                                 name, attempt + 1, _MAX_ATTEMPTS, code, e)

        if err is not None:
            _mark(name, False, (time.time() - t0) * 1000, err)
            errors.append(f'{name}: {err}')
            logger.warning('[KLine] %s failed for %s after %d attempts, '
                           'falling through: %s', name, code, _MAX_ATTEMPTS, err)
            continue

        if rows:
            _mark(name, True, (time.time() - t0) * 1000)
            logger.debug('[KLine] %s served %s (%d bars)', name, code, len(rows))
            return rows

        # Empty is treated as a source failure, not as "no such symbol", so we
        # still consult the next source before giving up.
        _mark(name, False, (time.time() - t0) * 1000, 'returned 0 bars')
        errors.append(f'{name}: 0 bars')
        logger.warning('[KLine] %s returned 0 bars for %s, falling through', name, code)

    logger.error('[KLine] ALL sources failed for %s: %s', code, '; '.join(errors))
    return []
