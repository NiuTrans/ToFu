"""lib/trading/_common.py — Shared session, proxy, and network state for trading package.

Design pattern: **Dependency Injection via TradingClient**

The ``TradingClient`` class encapsulates the HTTP session, proxy configuration,
and network-state logic that every trading sub-module needs.  A module-level
singleton ``_default_client`` is lazily created on first access (via PEP 562
``__getattr__``) to avoid opening HTTP sessions at import time.

Backward-compatible aliases (``_SESS``, ``_HEADERS``, ``_net_state``) are
also lazily resolved through the same mechanism.

For testing, instantiate ``TradingClient(proxy_url=None)`` directly — no monkeypatching
of module globals required.
"""

import os
import threading
import time

import requests

from lib.log import get_logger

logger = get_logger(__name__)

__all__ = [
    'TradingClient',
    '_get_default_client',
    '_check_external_network',
    'classify_asset_code',
    'is_stock_code',
    'is_etf_code',
    'is_fund_code',
    'stock_secid',
]


# ═══════════════════════════════════════════════════════════
#  Asset Code Classification
# ═══════════════════════════════════════════════════════════

def classify_asset_code(code: str) -> str:
    """Classify a 6-digit asset code into its type.

    Returns:
        'stock'  — A-share individual stocks
        'etf'    — Exchange-traded funds (51xxxx SH, 15xxxx/16xxxx SZ)
        'bond'   — Exchange-traded bonds (11xxxx, 12xxxx)
        'fund'   — Open-end funds (everything else)

    Stock patterns:
        60xxxx  — Shanghai main board
        601xxx, 603xxx, 605xxx — Shanghai main board sub-ranges
        000xxx  — Shenzhen main board
        001xxx  — Shenzhen main board (new codes since 2021, e.g. 001979 招商蛇口)
                  BUT 001xxx also includes many open-end funds (e.g. 001234).
                  Heuristic: 0010xx-0019xx are stocks, 001xxx with 3rd digit ≥2 are funds.
        002xxx  — Shenzhen SME board (中小板) — e.g. 002594 比亚迪
        003xxx  — Open-end funds (e.g. 003003)
        300xxx  — ChiNext board (创业板) — e.g. 300750 宁德时代
        68xxxx  — STAR Market (科创板) — e.g. 688981 中芯国际
    """
    if not code or len(code) != 6 or not code.isdigit():
        return 'fund'  # default fallback
    prefix2 = code[:2]
    prefix3 = code[:3]

    # Shanghai stocks
    if prefix2 == '60' or prefix2 == '68':
        return 'stock'
    # Shenzhen main board stocks: 000xxx
    if prefix3 == '000':
        return 'stock'
    # Shenzhen main board stocks: 001xxx (e.g. 001979 招商蛇口)
    # Distinguish from open-end funds: Shenzhen new main board codes
    # are 001800+ (started 2021); 001000-001799 are open-end funds.
    if prefix3 == '001' and code[3] >= '8':
        return 'stock'
    # Shenzhen SME board: 002xxx → stocks (e.g. 002594 比亚迪)
    if prefix3 == '002':
        return 'stock'
    # ChiNext: 300xxx → stocks (e.g. 300750 宁德时代)
    if prefix3 == '300' or prefix3 == '301':
        return 'stock'
    # ETFs
    if prefix2 in ('51', '15', '16'):
        return 'etf'
    # Exchange-traded bonds
    if prefix2 in ('11', '12'):
        return 'bond'
    # Everything else: open-end funds
    return 'fund'


def is_stock_code(code: str) -> bool:
    """Check if a code is an A-share individual stock."""
    return classify_asset_code(code) == 'stock'


def is_etf_code(code: str) -> bool:
    """Check if a code is an exchange-traded fund (ETF)."""
    return classify_asset_code(code) == 'etf'


def is_fund_code(code: str) -> bool:
    """Check if a code is an open-end (non-exchange-traded) fund."""
    return classify_asset_code(code) == 'fund'


def stock_secid(code: str) -> str:
    """Convert stock/ETF code to eastmoney secid format for push2 APIs.

    Shenzhen (深交所): codes starting with 0, 1, 3 → prefix '0.'
    Shanghai (上交所): codes starting with 5, 6 → prefix '1.'
    """
    if code and code[0] in ('0', '1', '3'):
        return f'0.{code}'
    return f'1.{code}'


# ═══════════════════════════════════════════════════════════
#  TradingClient — encapsulates session + proxy + network state
# ═══════════════════════════════════════════════════════════

class TradingClient:
    """HTTP client for trading data APIs with proxy support and network health checks.

    Attributes:
        session:     ``requests.Session`` pre-configured with headers & proxies.
        headers:     Default headers dict (User-Agent, Referer).
        proxy_url:   HTTPS proxy URL from environment, or ``None``.
        net_state:   Dict tracking last network probe time & reachability.
    """

    # Default headers for eastmoney / tiantian trading APIs
    DEFAULT_HEADERS = {
        'User-Agent': (
            'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) '
            'AppleWebKit/537.36 Chrome/121 Safari/537.36'
        ),
        'Referer': 'https://fund.eastmoney.com/',
    }

    def __init__(self, proxy_url=None, headers=None, probe_interval=120):
        """
        Args:
            proxy_url:      Explicit proxy URL, or ``None`` to read from env.
                            Pass ``""`` to force no-proxy (useful in tests).
            headers:        Override default headers.  ``None`` → use defaults.
            probe_interval: Seconds between network health probes.
        """
        # ── Headers ──
        self.headers = dict(headers or self.DEFAULT_HEADERS)

        # ── Proxy ──
        if proxy_url is None:
            # Auto-detect from environment
            proxy_url = (
                os.environ.get('HTTPS_PROXY')
                or os.environ.get('HTTP_PROXY')
                or os.environ.get('https_proxy')
                or os.environ.get('http_proxy')
                or ''
            )
        self.proxy_url = proxy_url

        # ── Session ──
        self.session = requests.Session()
        self.session.headers.update(self.headers)
        if self.proxy_url:
            self.session.proxies.update({
                'http': self.proxy_url,
                'https': self.proxy_url,
            })

        # ── Network state ──
        self._probe_interval = probe_interval
        self.net_state = {
            'reachable': True,
            'last_check': 0.0,
            'fail_count': 0,
        }
        self._net_lock = threading.Lock()

    def check_network(self) -> bool:
        """Fast check: is external network reachable?

        Probes at most once per ``probe_interval`` seconds.  Returns cached
        result between probes.  Thread-safe.
        """
        now = time.time()
        if now - self.net_state['last_check'] < self._probe_interval:
            return self.net_state['reachable']

        with self._net_lock:
            # Double-check after acquiring lock (another thread may have probed)
            if now - self.net_state['last_check'] < self._probe_interval:
                return self.net_state['reachable']

            try:
                self.session.head(
                    'http://fund.eastmoney.com/', timeout=5,
                    headers=self.headers,
                )
                self.net_state['reachable'] = True
                self.net_state['fail_count'] = 0
            except Exception as e:
                self.net_state['fail_count'] += 1
                self.net_state['reachable'] = False
                if self.net_state['fail_count'] <= 2:
                    logger.warning(
                        'External network probe failed (count=%d, proxy=%s): %s',
                        self.net_state['fail_count'],
                        self.proxy_url or 'direct', e,
                        exc_info=True)

            finally:
                self.net_state['last_check'] = now

            return self.net_state['reachable']


# ═══════════════════════════════════════════════════════════
#  Lazy singleton + backward-compatible aliases (PEP 562)
# ═══════════════════════════════════════════════════════════
#
# The singleton is NOT created at import time.  Instead, module-level
# ``__getattr__`` intercepts the first access to ``_default_client``,
# ``_HEADERS``, ``_SESS``, or ``_net_state`` and creates the TradingClient
# instance on demand.  This avoids opening HTTP sessions when the module
# is merely imported (e.g. during test collection or CLI parsing).

_lazy_client = None            # will hold the TradingClient once created
_lazy_lock = threading.Lock()


def _get_default_client() -> TradingClient:
    """Return (and lazily create) the module-level TradingClient singleton.

    Thread-safe: uses a module-level lock so only one thread creates the
    instance, even under concurrent first-access.
    """
    global _lazy_client
    # Fast path — already initialised
    if _lazy_client is not None:
        return _lazy_client

    with _lazy_lock:
        # Double-check after lock acquisition
        if _lazy_client is not None:
            return _lazy_client
        logger.debug('Lazily initialising default TradingClient singleton')
        _lazy_client = TradingClient()
    return _lazy_client


# ── PEP 562 module __getattr__ — makes lazy names importable ──

_LAZY_ALIASES = {
    '_default_client': lambda: _get_default_client(),
    '_HEADERS':        lambda: _get_default_client().headers,
    '_SESS':           lambda: _get_default_client().session,
    '_net_state':      lambda: _get_default_client().net_state,
}


def __getattr__(name: str):
    """Intercept attribute lookups for lazily-initialised module globals."""
    if name in _LAZY_ALIASES:
        value = _LAZY_ALIASES[name]()
        # Cache on the module so __getattr__ is not called again for this name
        globals()[name] = value
        return value
    raise AttributeError(f'module {__name__!r} has no attribute {name!r}')


def _check_external_network() -> bool:
    """Check external network reachability via the default client singleton."""
    return _get_default_client().check_network()
