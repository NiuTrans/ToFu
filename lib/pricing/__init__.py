"""
Pricing — model pricing tables, exchange rate fetching, and background updater.

Consolidated from lib/__init__.py (static tables) and server.py (dynamic fetchers)
to keep all pricing data and logic in one place.

This module is a pure re-export facade. Implementations live in sub-modules:

    ._tables    — DEFAULT_USD_CNY_RATE, MODEL_PRICING, QWEN_PRICING_CNY
    ._provider  — PROVIDER_PRICING registry + set/clear/lookup/snapshot
    ._refresh   — live pricing state + online refresh / exchange-rate fetchers

Public API (also re-exported by lib/__init__ for back-compat):
    MODEL_PRICING            — {model_id: {input, output, cacheWriteMul, cacheReadMul, name}}
    QWEN_PRICING_CNY         — {model_id: {input: [(threshold, cny_price)], output: [...]}}
    PROVIDER_PRICING         — {provider_id: {model_id: {input, output, ...}}} per-provider override
    DEFAULT_USD_CNY_RATE     — float
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
    MODEL_PRICING,
    QWEN_PRICING_CNY,
)

# ── Provider-scoped pricing registry ──
from lib.pricing._provider import (  # noqa: E402,F401
    PROVIDER_PRICING,
    _provider_pricing_lock,
    clear_provider_pricing,
    get_provider_pricing_snapshot,
    lookup_pricing,
    set_provider_pricing,
)

# ── Online refresh / exchange-rate fetchers + live pricing state ──
from lib.pricing._refresh import (  # noqa: E402,F401
    _do_update_pricing,
    _fetch_exchange_rate,
    _fetch_model_pricing_online,
    _pricing_data,
    _pricing_lock,
    _refresh_lock,
    _update_pricing_locked,
    get_pricing_data,
    refresh_pricing_async,
)

__all__ = [
    # tables
    'DEFAULT_USD_CNY_RATE',
    'MODEL_PRICING',
    'QWEN_PRICING_CNY',
    # provider registry
    'PROVIDER_PRICING',
    'set_provider_pricing',
    'clear_provider_pricing',
    'lookup_pricing',
    'get_provider_pricing_snapshot',
    # refresh
    'get_pricing_data',
    'refresh_pricing_async',
]
