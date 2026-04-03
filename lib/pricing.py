"""
Pricing — exchange rate fetching, model pricing lookup, and background updater.

Extracted from server.py to separate business logic from the HTTP layer.
"""

import json
import re
import threading
import time

import requests

import lib as _lib  # module ref for hot-reload
from lib.database import DOMAIN_SYSTEM, get_thread_db
from lib.log import get_logger
from lib.proxy import proxies_for as _proxies_for

logger = get_logger(__name__)

# ══════════════════════════════════════════════════════
#  Shared State
# ══════════════════════════════════════════════════════

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
            resp = requests.get(url, timeout=12, headers={'User-Agent': 'PricingBot/1.0'},
                               proxies=_proxies_for(url))
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
        resp = requests.get('https://openrouter.ai/api/v1/models', timeout=20,
                            headers={'User-Agent': 'PricingBot/1.0'},
                            proxies=_proxies_for('https://openrouter.ai/api/v1/models'))
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
        elif _lib.LLM_MODEL in _lib.MODEL_PRICING:
            mp = _lib.MODEL_PRICING[_lib.LLM_MODEL]
            _pricing_data.update(
                inputPrice=mp['input'], outputPrice=mp['output'],
                pricingSource='known_table', pricingUpdated=now_ms,
            )
        data_copy = dict(_pricing_data)
    # Persist to DB
    db = None
    try:
        db = get_thread_db(DOMAIN_SYSTEM)
        db.execute(
            'INSERT OR REPLACE INTO pricing_cache (key, value, updated_at) VALUES (?, ?, ?)',
            ('pricing', json.dumps(data_copy), now_ms),
        )
        db.commit()
    except Exception as e:
        logger.warning('[Pricing] failed to persist pricing to DB: %s', e, exc_info=True)

def _load_pricing_from_db():
    db = None
    try:
        db = get_thread_db(DOMAIN_SYSTEM)
        row = db.execute('SELECT value FROM pricing_cache WHERE key = ?', ('pricing',)).fetchone()
        if row:
            with _pricing_lock:
                _pricing_data.update(json.loads(row['value']))
            return True
    except Exception as e:
        logger.warning('[Pricing] failed to load cached pricing from DB: %s', e, exc_info=True)
    return False

# ══════════════════════════════════════════════════════
#  Background Worker
# ══════════════════════════════════════════════════════

