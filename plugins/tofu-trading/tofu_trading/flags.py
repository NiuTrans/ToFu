"""tofu_trading/flags.py — ``tofu.flags`` entry-point registrar.

Declares the ``trading_enabled`` feature flag so the host's feature-flag
registry, ``/api/v1/features`` / ``/api/v1/capabilities`` surfaces, and the
Settings UI know the flag exists without core hardcoding ``TRADING_ENABLED``.

The registrar is intentionally tolerant of the host not yet exposing a flag
registry (the seam lands in a later core release): it tries the registry and
no-ops with a debug log otherwise, so an older host still loads the plugin.
"""

from __future__ import annotations

from lib.log import get_logger

logger = get_logger(__name__)

# (env var, features.json key, default). Mirrors the historical core constant
# TRADING_ENABLED = _resolve_feature_flag('TRADING_ENABLED', 'trading_enabled', False).
FLAG = ('TRADING_ENABLED', 'trading_enabled', False)


def register(register_feature_flag=None):
    """Entry point: declare the trading feature flag with the host.

    Args:
        register_feature_flag: the host's flag-registration callable, passed
            in by the host's ``tofu.flags`` discovery.  When None (older host
            without the seam), this is a no-op.
    """
    if register_feature_flag is None:
        logger.debug('[tofu-trading] host has no feature-flag registry; '
                     'trading_enabled not registered (using legacy core constant)')
        return
    env_key, json_key, default = FLAG
    # needs_restart=False: the flag is read live (tofu_trading.gate) by the
    # request-time blueprint guard and by both background workers, so a toggle
    # takes effect immediately. It was True while the blueprints were mounted
    # unconditionally and nothing read the flag — which made the Settings
    # toggle claim a restart was needed for a change that, in reality, never
    # happened at all.
    register_feature_flag(env_key=env_key, json_key=json_key, default=default,
                          needs_restart=False)
    logger.info('[tofu-trading] registered feature flag %s', json_key)


__all__ = ['register', 'FLAG']
