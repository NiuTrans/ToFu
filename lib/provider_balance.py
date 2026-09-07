"""Provider balance/billing response normalizer.

Pure engine moved out of ``routes/config.py`` (2026-06): takes a provider's
raw billing JSON and flattens the many vendor-specific shapes (OpenAI
subscription, DeepSeek balance_infos, OpenRouter credits, generic) into one
unified dict. No Flask dependency. ``routes/config.py`` re-exports
``normalize_balance`` as ``_normalize_balance`` and keeps the HTTP route
handler (request parse + error→response shaping).
"""

from lib.log import get_logger

logger = get_logger(__name__)


def normalize_balance(billing, balance_url, headers):
    """Normalize different provider balance formats into a unified structure.

    Unified output fields (all optional):
      - balance_usd: remaining balance in USD
      - used_usd: total used in USD
      - limit_usd: total limit/quota in USD
      - currency: original currency if non-USD
      - balance_local: remaining in original currency
      - hard_limit_usd / total_usage_cents: legacy OpenAI format
      - raw: original response if nothing else matched
    """
    from lib.http_client import http_get
    result = {}

    # ── Format 1: OpenAI /subscription style (hard_limit_usd) ──
    if 'hard_limit_usd' in billing:
        result['hard_limit_usd'] = billing['hard_limit_usd']
        result['limit_usd'] = billing['hard_limit_usd']
        result['soft_limit_usd'] = billing.get('soft_limit_usd')

        if balance_url.endswith('/subscription'):
            usage_url = balance_url.rsplit('/subscription', 1)[0] + '/usage'
            try:
                uresp = http_get(usage_url, headers=headers, timeout=15)
                uresp.raise_for_status()
                usage_data = uresp.json()
                if 'total_usage' in usage_data:
                    result['total_usage_cents'] = usage_data['total_usage']
                    result['used_usd'] = usage_data['total_usage'] / 100
                    result['balance_usd'] = result['limit_usd'] - result['used_usd']
            except Exception as e:
                logger.debug('[Balance] Usage fetch from %s failed (non-critical): %s', usage_url, e)
        return result

    # ── Format 2: DeepSeek /user/balance (balance_infos array) ──
    if 'balance_infos' in billing:
        infos = billing.get('balance_infos', [])
        result['is_available'] = billing.get('is_available', True)
        if infos:
            # Prefer USD, fallback to first entry
            info = infos[0]
            for bi in infos:
                if bi.get('currency', '').upper() == 'USD':
                    info = bi
                    break
            currency = info.get('currency', 'CNY')
            total = float(info.get('total_balance', 0))
            granted = float(info.get('granted_balance', 0))
            topped_up = float(info.get('topped_up_balance', 0))
            result['currency'] = currency
            result['balance_local'] = total
            result['granted_balance'] = granted
            result['topped_up_balance'] = topped_up
            # Approximate USD if CNY
            if currency.upper() == 'USD':
                result['balance_usd'] = total
            else:
                result['balance_usd'] = round(total / 7.2, 2)  # approximate CNY→USD
        return result

    # ── Format 3: OpenRouter /credits (data.total_credits / total_usage) ──
    credits_data = billing.get('data', billing)
    if 'total_credits' in credits_data:
        tc = float(credits_data.get('total_credits', 0))
        tu = float(credits_data.get('total_usage', 0))
        result['limit_usd'] = round(tc, 4)
        result['used_usd'] = round(tu, 4)
        result['balance_usd'] = round(tc - tu, 4)
        return result

    # ── Format 4: Generic — look for common field names ──
    for key in ('balance', 'remaining', 'credits', 'available_balance'):
        if key in billing:
            val = billing[key]
            if isinstance(val, (int, float)):
                result['balance_usd'] = float(val)
                return result
            if isinstance(val, str):
                try:
                    result['balance_usd'] = float(val)
                    return result
                except (ValueError, TypeError) as e:
                    logger.debug('[Config] balance_usd parse failed for key=%s: %s', key, e)

    # ── Fallback: return raw data ──
    result['raw'] = billing
    return result


__all__ = ['normalize_balance']
