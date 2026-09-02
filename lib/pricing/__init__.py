"""
Pricing — model pricing tables, exchange rate fetching, and background updater.

Consolidated from lib/__init__.py (static tables) and server.py (dynamic fetchers)
to keep all pricing data and logic in one place.

This module is a pure re-export facade. Implementations live in sub-modules:

    ._tables    — default USD display rates + model pricing tables
    ._provider  — PROVIDER_PRICING registry + set/clear/lookup/snapshot
    ._refresh   — live pricing state + online refresh / exchange-rate fetchers

Public API (also re-exported by lib/__init__ for back-compat):
    MODEL_PRICING            — {model_id: {input, output, cacheWriteMul, cacheReadMul, name}}
    QWEN_PRICING_CNY         — {model_id: {input: [(threshold, cny_price)], output: [...]}}
    PROVIDER_PRICING         — {provider_id: {model_id: {input, output, ...}}} per-provider override
    DEFAULT_USD_CNY_RATE     — float
    DEFAULT_USD_DISPLAY_RATES — bounded Settings presentation pivots
    get_model_price_display_policy() — browser-safe rates + freshness metadata
    get_pricing_data()       — thread-safe copy of live pricing state
    lookup_pricing(model, provider_id=None) — provider-scoped pricing resolution
    set_provider_pricing(provider_id, model_id, info) — register one override
    clear_provider_pricing(provider_id) — drop a provider's overrides
    get_provider_pricing_snapshot()     — snapshot the override map
    refresh_pricing_async()  — trigger background pricing refresh
"""

# ── Static pricing tables ──
from lib.pricing._tables import (  # noqa: E402,F401
    DEFAULT_USD_CNY_RATE,
    DEFAULT_USD_DISPLAY_RATES,
    MODEL_PRICING,
    QWEN_PRICING_CNY,
)

# ── Provider-scoped pricing registry ──
from lib.pricing._provider import (  # noqa: E402,F401
    PROVIDER_PRICING,
    _provider_pricing_lock,
    build_rate_card,
    clear_provider_pricing,
    get_provider_pricing_snapshot,
    lookup_pricing,
    normalize_pricing,
    set_provider_pricing,
)

# ── Peak/off-peak price schedules ──
from lib.pricing._peak import peak_multiplier  # noqa: E402,F401

# ── Online refresh / exchange-rate fetchers + live pricing state ──
from lib.pricing._refresh import (  # noqa: E402,F401
    _do_update_pricing,
    _fetch_exchange_rate,
    _fetch_exchange_rates,
    _fetch_model_pricing_online,
    _pricing_data,
    _pricing_lock,
    _refresh_lock,
    _update_pricing_locked,
    get_model_price_display_policy,
    get_pricing_data,
    refresh_pricing_async,
    stop_pricing_refresh,
)

__all__ = [
    # tables
    'DEFAULT_USD_CNY_RATE',
    'DEFAULT_USD_DISPLAY_RATES',
    'MODEL_PRICING',
    'QWEN_PRICING_CNY',
    # provider registry
    'PROVIDER_PRICING',
    'set_provider_pricing',
    'clear_provider_pricing',
    'lookup_pricing',
    'get_provider_pricing_snapshot',
    'normalize_pricing',
    'build_rate_card',
    # peak schedules
    'peak_multiplier',
    # refresh
    'get_model_price_display_policy',
    'get_pricing_data',
    'refresh_pricing_async',
    'stop_pricing_refresh',
]
