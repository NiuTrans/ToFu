"""
Pricing — online price/exchange-rate refresh and background updater.

Owns the live pricing state and the background refresh machinery:
    _pricing_data     — live pricing dict (thread-safe copy via get_pricing_data)
    _pricing_lock     — guards _pricing_data
    _refresh_lock     — dedups concurrent refreshes
    get_pricing_data()        — thread-safe copy of live pricing state
    refresh_pricing_async()   — trigger background pricing refresh (non-blocking)

Internal fetchers (_fetch_exchange_rate / _fetch_model_pricing_online) and
the updater (_update_pricing_locked / _do_update_pricing) live here too.
``_pricing_data`` is mutated in place under ``_pricing_lock`` by
``_do_update_pricing``; keeping the dict + lock + updater in one module
guarantees they share the same object by reference.
"""

import re
import threading
import time
import uuid

from lib.log import get_logger
from lib.http_client import http_get

from lib.pricing._tables import MODEL_PRICING

logger = get_logger(__name__)



_pricing_lock = threading.Lock()
_refresh_lock = threading.Lock()  # Guards refresh dedup — acquire(blocking=False) for non-blocking skip
_refresh_owner_lock = threading.Lock()
_refresh_stop = threading.Event()
_refresh_thread = None
_pricing_data = {
    'model': '', 'inputPrice': 15.0, 'outputPrice': 75.0,  # model populated at runtime
    'cacheWriteMul': 1.25, 'cacheReadMul': 0.10,
    'usdToCny': 7.24, 'exchangeRateUpdated': 0,  # DEFAULT_USD_CNY_RATE read at runtime
    'pricingUpdated': 0, 'pricingSource': 'default',
    'exchangeRateSource': 'none', 'onlineMatchedModel': None,
}


def _storage(*, write: bool = False):
    from lib.storage import get_storage_client
    return get_storage_client(write=write)

# ══════════════════════════════════════════════════════
#  Public API
# ══════════════════════════════════════════════════════

def get_pricing_data():
    """Return a thread-safe copy of the current pricing data."""
    with _pricing_lock:
        return dict(_pricing_data)


def refresh_pricing_async():
    """Trigger the single owned pricing refresh worker, without blocking."""
    global _refresh_thread
    if not _refresh_lock.acquire(blocking=False):
        logger.debug('[Pricing] Refresh already in progress — skipping duplicate request')
        return
    thread = None
    try:
        with _refresh_owner_lock:
            _refresh_stop.clear()
            thread = threading.Thread(
                target=_update_pricing_locked,
                daemon=True,
                name='pricing-refresh',
            )
            _refresh_thread = thread
            thread.start()
    except Exception:
        logger.error('[Pricing] Failed to start pricing refresh thread', exc_info=True)
        with _refresh_owner_lock:
            if thread is not None and _refresh_thread is thread:
                _refresh_thread = None
        _refresh_lock.release()
        raise


def stop_pricing_refresh(timeout=2.0):
    """Signal and bounded-join an in-flight refresh during app shutdown."""
    global _refresh_thread
    _refresh_stop.set()
    with _refresh_owner_lock:
        thread = _refresh_thread
    if thread is None:
        return True
    try:
        wait_seconds = max(0.0, float(timeout))
    except (TypeError, ValueError, OverflowError) as exc:
        logger.debug('[Pricing] invalid stop timeout; using 2.0: %s', exc)
        wait_seconds = 2.0
    if thread is not threading.current_thread():
        thread.join(timeout=wait_seconds)
    if thread.is_alive():
        return False
    with _refresh_owner_lock:
        if _refresh_thread is thread:
            _refresh_thread = None
    return True

# ══════════════════════════════════════════════════════
#  Internal Fetchers
# ══════════════════════════════════════════════════════

def _fetch_exchange_rate():
    apis = [
        ('https://api.exchangerate-api.com/v4/latest/USD', lambda d: d.get('rates', {}).get('CNY')),
        ('https://open.er-api.com/v6/latest/USD', lambda d: d.get('rates', {}).get('CNY')),
        ('https://cdn.jsdelivr.net/npm/@fawazahmed0/currency-api@latest/v1/currencies/usd.json', lambda d: d.get('usd', {}).get('cny')),
    ]
    for url, extract in apis:
        try:
            resp = http_get(url, timeout=12, headers={'User-Agent': 'PricingBot/1.0'})
            if resp.ok:
                rate = extract(resp.json())
                if rate and float(rate) > 0:
                    return round(float(rate), 4)
        except Exception as e:
            logger.warning('[Pricing] exchange rate API %s failed: %s', url, e, exc_info=True)
    return None

def _fetch_model_pricing_online(model_name):
    try:
        norm = model_name.lower()
        for prefix in ('aws.', 'gcp.', 'azure.', 'bedrock.'):
            norm = norm.replace(prefix, '')
        norm = re.sub(r'\.\d+$', '', norm)
        resp = http_get('https://openrouter.ai/api/v1/models', timeout=20,
                            headers={'User-Agent': 'PricingBot/1.0'})
        if not resp.ok:
            return None
        norm_parts = set(norm.replace('-', ' ').replace('.', ' ').split())
        best, best_score = None, 0
        for m in resp.json().get('data', []):
            mid = m.get('id', '').lower()
            mid_short = mid.split('/')[-1] if '/' in mid else mid
            overlap = len(norm_parts & set(mid_short.replace('-', ' ').replace('.', ' ').split()))
            if overlap < 2:
                continue
            pricing = m.get('pricing', {})
            pp = float(pricing.get('prompt', 0) or 0)
            cp = float(pricing.get('completion', 0) or 0)
            if pp <= 0 and cp <= 0:
                continue
            if overlap > best_score:
                best_score = overlap
                best = {
                    'input': round(pp * 1e6, 4),
                    'output': round(cp * 1e6, 4),
                    'matched': m.get('id', ''),
                }
        return best
    except Exception as e:
        logger.warning('[Pricing] OpenRouter model pricing fetch failed for %s: %s', model_name, e, exc_info=True)
        return None

def _update_pricing_locked():
    """Wrapper that owns _refresh_lock; used only by refresh_pricing_async."""
    global _refresh_thread
    from lib.observability import background_job_finished, background_job_started
    started_at = time.monotonic()
    outcome = 'success'
    background_job_started('pricing_refresh')
    try:
        _do_update_pricing()
        if _refresh_stop.is_set():
            outcome = 'cancelled'
    except BaseException:
        outcome = 'error'
        raise
    finally:
        background_job_finished(
            'pricing_refresh', outcome, time.monotonic() - started_at)
        with _refresh_owner_lock:
            if _refresh_thread is threading.current_thread():
                _refresh_thread = None
        _refresh_lock.release()

def _do_update_pricing():
    import lib as _lib  # deferred to avoid circular import
    if _refresh_stop.is_set():
        return
    now_ms = int(time.time() * 1000)
    rate = _fetch_exchange_rate()
    if _refresh_stop.is_set():
        return
    online = _fetch_model_pricing_online(_lib.LLM_MODEL)
    if _refresh_stop.is_set():
        return
    with _pricing_lock:
        if rate:
            _pricing_data['usdToCny'] = rate
            _pricing_data['exchangeRateUpdated'] = now_ms
            _pricing_data['exchangeRateSource'] = 'api'
        if online:
            _pricing_data.update(
                inputPrice=online['input'], outputPrice=online['output'],
                pricingSource='openrouter', onlineMatchedModel=online['matched'],
                pricingUpdated=now_ms,
            )
        elif _lib.LLM_MODEL in MODEL_PRICING:
            mp = MODEL_PRICING[_lib.LLM_MODEL]
            _pricing_data.update(
                inputPrice=mp['input'], outputPrice=mp['output'],
                pricingSource='known_table', pricingUpdated=now_ms,
            )
        data_copy = dict(_pricing_data)
    # Regenerable cache: one semantic Sidecar write, never a local connection.
    try:
        _storage(write=True).command('record.put', {
            'namespace': 'pricing_cache', 'key': 'pricing',
            'value': data_copy,
        }, f'pricing-cache:{uuid.uuid4().hex}', priority='event')
    except Exception as e:
        logger.warning(
            '[Pricing] failed to persist pricing cache: %s',
            type(e).__name__)

# ══════════════════════════════════════════════════════
#  Background Worker
# ══════════════════════════════════════════════════════
