"""lib/llm_dispatch/discovery/_balance.py — Balance/billing URL auto-detection."""

from urllib.parse import urlparse

import requests

from lib.http_client import http_get
from lib.log import get_logger

logger = get_logger(__name__)


# ══════════════════════════════════════════════════════
#  Balance URL Auto-Detection
# ══════════════════════════════════════════════════════

# Common billing/balance endpoint paths to probe (order: most common first)
_BALANCE_PROBE_PATHS = [
    '/dashboard/billing/subscription',
    '/v1/dashboard/billing/subscription',
    '/billing/credit/grants',
    '/user/balance',
    '/balance',
]

_BALANCE_PROBE_TIMEOUT = 5  # seconds per path


def _probe_balance_url(base_url: str, api_key: str) -> str:
    """Try common balance/billing URL patterns and return the first working one.

    Only sends GET requests with short timeouts. Returns empty string if
    no working balance endpoint is found.

    Args:
        base_url: Provider API base URL.
        api_key: API key for authorization.

    Returns:
        Working balance URL, or empty string.
    """
    parsed = urlparse(base_url.rstrip('/'))
    origin = '%s://%s' % (parsed.scheme, parsed.netloc)
    # Use-time SSRF egress guard — the origin is what we'll actually hit.
    from lib.byo_egress import EgressDenied, validate_egress_url
    try:
        validate_egress_url(origin)
    except EgressDenied as e:
        logger.warning('[BalanceProbe] blocked egress to %s: %s', origin, e)
        return ''
    headers = {
        'Authorization': 'Bearer %s' % api_key,
        'User-Agent': 'Tofu/1.0',
    }

    for path in _BALANCE_PROBE_PATHS:
        probe_url = origin + path
        try:
            resp = http_get(
                probe_url,
                headers=headers,
                timeout=_BALANCE_PROBE_TIMEOUT,
                allow_redirects=False,
            )
            if resp.ok:
                # Verify it returns JSON (not an HTML error page)
                ct = resp.headers.get('content-type', '')
                if 'json' in ct:
                    try:
                        resp.json()
                        logger.info('[BalanceProbe] Found working balance URL: %s', probe_url)
                        return probe_url
                    except (ValueError, TypeError) as e:
                        logger.debug('[BalanceProbe] %s returned non-JSON body: %s', probe_url, e)
            logger.debug('[BalanceProbe] %s returned HTTP %d (skipped)', probe_url, resp.status_code)
        except requests.Timeout:
            logger.debug('[BalanceProbe] %s timed out', probe_url)
        except requests.RequestException as e:
            logger.debug('[BalanceProbe] %s failed: %s', probe_url, e)

    logger.info('[BalanceProbe] No working balance endpoint found for %s', origin)
    return ''
