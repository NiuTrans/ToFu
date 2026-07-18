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

import json
import re
import threading
import time

from lib.log import get_logger
from lib.http_client import http_get

from lib.pricing._tables import MODEL_PRICING

logger = get_logger(__name__)



_pricing_lock = threading.Lock()
_refresh_lock = threading.Lock()  # Guards refresh dedup — acquire(blocking=False) for non-blocking skip
_pricing_data = {
    'model': '', 'inputPrice': 15.0, 'outputPrice': 75.0,  # model populated at runtime
    'cacheWriteMul': 1.25, 'cacheReadMul': 0.10,
    'usdToCny': 7.24, 'exchangeRateUpdated': 0,  # DEFAULT_USD_CNY_RATE read at runtime
    'pricingUpdated': 0, 'pricingSource': 'default',
    'exchangeRateSource': 'none', 'onlineMatchedModel': None,
}

# ══════════════════════════════════════════════════════
#  Public API
# ══════════════════════════════════════════════════════

def get_pricing_data():
    """Return a thread-safe copy of the current pricing data."""
    with _pricing_lock:
        return dict(_pricing_data)


def refresh_pricing_async():
    """Trigger a background pricing refresh. Non-blocking, deduped."""
    if not _refresh_lock.acquire(blocking=False):
        logger.debug('[Pricing] Refresh already in progress — skipping duplicate request')
        return
    try:
        threading.Thread(target=_update_pricing_locked, daemon=True).start()
    except Exception:
        logger.error('[Pricing] Failed to start pricing refresh thread', exc_info=True)
        _refresh_lock.release()
        raise

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
    try:
        _do_update_pricing()
    finally:
        _refresh_lock.release()

def _do_update_pricing():
    import lib as _lib  # deferred to avoid circular import
    now_ms = int(time.time() * 1000)
    rate = _fetch_exchange_rate()
    online = _fetch_model_pricing_online(_lib.LLM_MODEL)
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
    # Persist to DB
    db = None
    try:
        from lib.database import DOMAIN_SYSTEM, get_thread_db
        from lib.database._core_schema import PRICING_CACHE, upsert
        db = get_thread_db(DOMAIN_SYSTEM)
        # Backend-agnostic UPSERT (replaces INSERT OR REPLACE + _PK_MAP regex
        # translation). conflict_cols defaults to the PK ('key').
        upsert(db, PRICING_CACHE,
               {'key': 'pricing', 'value': json.dumps(data_copy), 'updated_at': now_ms},
               commit=True)
    except Exception as e:
        logger.warning('[Pricing] failed to persist pricing to DB: %s', e, exc_info=True)

# ══════════════════════════════════════════════════════
#  Background Worker
# ══════════════════════════════════════════════════════

