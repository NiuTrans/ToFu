"""lib.billing.pricing — Relay margin + (legacy) per-model price table.

★ As of the 2026-06-24 single-engine unification, the per-token RATE math
no longer lives here. ``lib/billing/cost.compute_request_cost`` delegates to
``lib.cost.compute_cost`` over the rich ``lib/pricing.py`` table (100+ models,
cache conventions, Qwen tiers, provider overrides). What this module STILL
owns for billing is the relay **margin** (``get_default_margin`` ← the
``default_margin`` field of ``pricing.json``).

The per-model ``*_per_mtok_micro`` rows + :func:`get_price` / :class:`ModelPrice`
are now READ-ONLY legacy: surfaced by :func:`list_prices` for the public
rate-card display only, and NOT used for any cost/debit calculation. There is
NO writer for them anymore — :func:`save_margin` persists ONLY the relay
margin. The authoritative cost rates live in ``lib/pricing.py`` (the single
cost engine ``lib.cost.compute_cost`` reads them); this avoids a second,
driftable rate table. Don't reintroduce a rate writer here.

Loaded from ``data/config/pricing.json`` (created on first read with
sensible defaults). Hot-reloadable: a ``mtime`` check on each lookup
invalidates the in-process cache so admin edits take effect without
restart.

Schema (``pricing.json``)::

    {
      "version": 1,
      "currency": "USD",
      "default_margin": 0.20,
      "default_model": {
        "input_per_mtok_micro":  3000000,
        "output_per_mtok_micro": 15000000,
        "cache_read_per_mtok_micro": 300000
      },
      "models": {
        "gpt-4o-mini":      {"input_per_mtok_micro":  150000,
                              "output_per_mtok_micro": 600000},
        "claude-3-5-sonnet": {"input_per_mtok_micro": 3000000,
                              "output_per_mtok_micro":15000000,
                              "cache_read_per_mtok_micro": 300000}
      }
    }

Units
-----
Prices are in **micro-credits per million tokens** (per-Mtok, micro
credits). One credit ≈ US $0.001. So an OpenAI advertised price of
$0.15 / Mtok input maps to 150_000 micro-credits / Mtok at the default
1000-credits-per-dollar conversion. The relay margin is added on the
fly in :mod:`lib.billing.cost` — it is NOT baked into the table so a
single edit changes both display price and bill.

Lookup precedence
-----------------
1. Exact match in ``models``.
2. Family match: prefix-match on ``models`` keys, longest first
   (``"claude-3-5-sonnet-20241022"`` → ``"claude-3-5-sonnet"``).
3. ``default_model`` block. Logged at WARN so the operator knows to
   add an explicit row.
"""

from __future__ import annotations

import os
import threading
from copy import deepcopy
from dataclasses import dataclass
from typing import Dict, Optional

from lib.config_dir import config_path
from lib.json_store import (
    JsonStoreReadError,
    read_json,
    update_json_atomic,
)
from lib.log import get_logger

logger = get_logger(__name__)

_PRICING_PATH = config_path('pricing.json')
_PRICING_VERSION = 1

# Sensible defaults seeded on first read. Numbers are micro-credits per
# million tokens at the canonical 1 credit = $0.001 conversion.
_DEFAULT_PRICING: Dict = {
    'version': _PRICING_VERSION,
    'currency': 'USD',
    'default_margin': 0.20,
    'default_model': {
        'input_per_mtok_micro': 3_000_000,
        'output_per_mtok_micro': 15_000_000,
        'cache_read_per_mtok_micro': 300_000,
        'cache_write_per_mtok_micro': 3_750_000,
    },
    'models': {
        # OpenAI public list prices (≈ 2025-Q1)
        'gpt-4o':              {'input_per_mtok_micro':  2_500_000,
                                'output_per_mtok_micro': 10_000_000},
        'gpt-4o-mini':         {'input_per_mtok_micro':    150_000,
                                'output_per_mtok_micro':    600_000},
        'gpt-4-turbo':         {'input_per_mtok_micro': 10_000_000,
                                'output_per_mtok_micro': 30_000_000},
        # Anthropic public list prices
        'claude-3-5-sonnet':   {'input_per_mtok_micro':  3_000_000,
                                'output_per_mtok_micro': 15_000_000,
                                'cache_read_per_mtok_micro':   300_000,
                                'cache_write_per_mtok_micro': 3_750_000},
        'claude-3-5-haiku':    {'input_per_mtok_micro':    800_000,
                                'output_per_mtok_micro':  4_000_000},
        'claude-3-opus':       {'input_per_mtok_micro': 15_000_000,
                                'output_per_mtok_micro': 75_000_000},
        # Local-deployment placeholder (operator should set their cost)
        'local':               {'input_per_mtok_micro':         0,
                                'output_per_mtok_micro':        0},
    },
}


@dataclass(frozen=True)
class ModelPrice:
    """Per-model unit prices, in micro-credits per million tokens."""
    model: str
    input_per_mtok_micro: int
    output_per_mtok_micro: int
    cache_read_per_mtok_micro: int = 0
    cache_write_per_mtok_micro: int = 0
    matched: str = ''  # which key in pricing.json was hit (for diagnostics)


_lock = threading.RLock()
_cache: Optional[Dict] = None
_cache_mtime: float = 0.0


def _file_mtime() -> float:
    try:
        return os.path.getmtime(_PRICING_PATH)
    except OSError as e:
        logger.debug('[Pricing] mtime probe failed for %s: %s', _PRICING_PATH, e)
        return 0.0


def _ensure_loaded() -> Dict:
    global _cache, _cache_mtime
    with _lock:
        mtime = _file_mtime()
        if _cache is not None and mtime == _cache_mtime:
            return _cache
        if mtime == 0.0:
            # First read — seed the file.
            try:
                def _seed(current):
                    if current is None:
                        return deepcopy(_DEFAULT_PRICING)
                    if not isinstance(current, dict):
                        raise JsonStoreReadError(
                            'pricing store is not a JSON object')
                    return current

                update_json_atomic(
                    _PRICING_PATH, _seed, default=None, strict=True)
                logger.info('[Pricing] Seeded %s with defaults',
                            _PRICING_PATH)
            except (OSError, JsonStoreReadError) as e:
                logger.warning('[Pricing] Could not seed %s: %s — '
                               'using in-memory defaults',
                               _PRICING_PATH, e)
                _cache = deepcopy(_DEFAULT_PRICING)
                _cache_mtime = 0.0
                return _cache
            mtime = _file_mtime()
        raw = read_json(_PRICING_PATH, default=_DEFAULT_PRICING)
        if not isinstance(raw, dict) or 'default_model' not in raw:
            logger.warning('[Pricing] %s is malformed — using defaults',
                           _PRICING_PATH)
            raw = dict(_DEFAULT_PRICING)
        _cache = raw
        _cache_mtime = mtime
        return _cache


def reload_pricing() -> None:
    """Force a reload on the next ``get_price()`` call."""
    global _cache_mtime
    with _lock:
        _cache_mtime = -1.0


def _coerce_row(model: str, row: Dict, matched: str) -> ModelPrice:
    return ModelPrice(
        model=model,
        input_per_mtok_micro=int(row.get('input_per_mtok_micro') or 0),
        output_per_mtok_micro=int(row.get('output_per_mtok_micro') or 0),
        cache_read_per_mtok_micro=int(row.get('cache_read_per_mtok_micro') or 0),
        cache_write_per_mtok_micro=int(row.get('cache_write_per_mtok_micro') or 0),
        matched=matched,
    )


def get_price(model: str) -> ModelPrice:
    """Look up the price row for ``model``.

    Falls back through family-prefix matching to ``default_model``.
    Always returns a :class:`ModelPrice`; never raises on unknown
    models (logs a warning instead so the request still bills).
    """
    cfg = _ensure_loaded()
    models = cfg.get('models') or {}
    if model in models:
        return _coerce_row(model, models[model], matched=model)
    # Longest-prefix family match.
    best_key = ''
    for key in models.keys():
        if model.startswith(key) and len(key) > len(best_key):
            best_key = key
    if best_key:
        return _coerce_row(model, models[best_key], matched=best_key)
    # Fallback.
    logger.warning('[Pricing] No row for model=%r — using default_model. '
                   'Add an entry to %s to remove this warning.',
                   model, _PRICING_PATH)
    return _coerce_row(model, cfg.get('default_model') or {},
                       matched='default_model')


def list_prices() -> Dict:
    """Return the full pricing payload (for the admin UI)."""
    # This is a cache boundary, not an invitation to mutate the live nested
    # ``models``/``default_model`` dicts behind get_price().
    return deepcopy(_ensure_loaded())


def get_default_margin() -> float:
    """Return the relay's profit margin as a fraction (e.g. 0.2 = +20%)."""
    cfg = _ensure_loaded()
    return float(cfg.get('default_margin') or 0.0)


class PricingError(ValueError):
    """Raised when a proposed margin value fails validation."""


def save_margin(margin: float) -> Dict:
    """Persist ONLY the relay profit margin, then hot-reload.

    ★ As of the 2026-06-24 single-engine unification the per-model RATE rows
    are no longer an editable surface — they are NOT a cost source anymore
    (cost rates are authoritative in ``lib/pricing.py``), so allowing edits
    here would re-create the very drift this cleanup removed. The ONLY tunable
    that still affects billing is the relay markup, so this writer touches
    ``default_margin`` and leaves every other field of ``pricing.json``
    (``default_model`` / ``models`` — kept for the read-only rate-card display
    + legacy compatibility) byte-identical.

    Args:
        margin: Relay markup as a fraction (0.20 = +20%). 0.0 disables markup.

    Returns:
        The full persisted payload (same shape :func:`list_prices` returns).

    Raises:
        :class:`PricingError` when ``margin`` is not a number in [0, 100].
        Nothing is written when validation fails.
    """
    try:
        margin = float(margin)
    except (TypeError, ValueError):
        raise PricingError('default_margin: must be a number')
    if not (0.0 <= margin <= 100.0):
        raise PricingError('default_margin: must be between 0 and 100')

    # Merge against the latest on-disk document while holding both the module
    # cache lock and json_store's cross-process RMW lock.  The former
    # load-cache-edit-write path could erase an operator's concurrent rate-card
    # or metadata edit with a stale snapshot even though this API promises to
    # touch only default_margin.
    global _cache, _cache_mtime
    with _lock:
        def _mutate(current):
            if current is None:
                current = deepcopy(_DEFAULT_PRICING)
            if not isinstance(current, dict):
                raise JsonStoreReadError(
                    'pricing store is not a JSON object')
            current['default_margin'] = margin
            current.setdefault('version', _PRICING_VERSION)
            current.setdefault('currency', 'USD')
            return current

        cfg = update_json_atomic(
            _PRICING_PATH, _mutate, default=None, strict=True)
        _cache = deepcopy(cfg)
        _cache_mtime = _file_mtime()
    logger.info('[Pricing] Saved relay margin=%.3f to %s (rate rows untouched '
                '— authoritative rates live in lib/pricing.py)',
                margin, _PRICING_PATH)
    return deepcopy(cfg)


__all__ = [
    'ModelPrice', 'get_price', 'list_prices', 'reload_pricing',
    'save_margin', 'PricingError', 'get_default_margin',
]
