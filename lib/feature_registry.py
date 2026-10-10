"""lib/feature_registry.py — Pluggable boolean feature flags.

Mirror of the other plugin seams (``tofu.tools`` / ``tofu.providers`` /
``tofu.blueprints`` / the schema entry-point seam).  Lets a third-party package declare a
boolean feature flag without core hardcoding it.

Why this exists
---------------
Core historically hardcoded ``TRADING_ENABLED`` in three places: the constant +
``reload_config`` in ``lib/__init__.py``, the ``_BOOL_FLAGS`` list in
``lib/features_store.py``, and ``_features()`` in
``routes/api_v1/capabilities.py`` / ``routes/common.py``.  That baked an
optional feature's flag into core and blocked extracting trading.

A plugin now declares its flag via the ``tofu.flags`` entry point::

    [project.entry-points."tofu.flags"]
    trading = "tofu_trading.flags:register"

where ``register(register_feature_flag)`` calls
:func:`register_feature_flag` with the flag's metadata.  Core's flag-consuming
code iterates ``base flags + registered flags`` instead of naming any plugin
flag.

Design contract
---------------
1. **Base flags stay in core.**  ``pptx_translate_enabled``, ``debug_mode``,
   ``cache_extended_ttl``, ``optimizer_enabled``, ``artifacts_enabled`` are
   core's own and are NOT registered here — they remain hardcoded in
   ``lib/__init__.py``.  Only OPTIONAL plugin flags register.
2. **needs_restart is declared per-flag.**  A flag whose feature mounts
   blueprints at import time (trading) sets ``needs_restart=True`` so the
   ``POST /api/v1/features`` handler can tell the user a flip won't take effect
   until restart when the plugin's routes weren't registered at boot.
3. **Fail-soft discovery.**  A plugin that errors on load is logged + skipped.
"""

from __future__ import annotations

from dataclasses import dataclass

from lib.log import get_logger

logger = get_logger(__name__)

_ENTRY_POINT_GROUP = 'tofu.flags'


@dataclass(frozen=True)
class FeatureFlag:
    """Metadata for a plugin-declared boolean feature flag.

    Attributes:
        env_key: Environment-variable name (e.g. ``TRADING_ENABLED``).
        json_key: Key in ``data/config/features.json`` (e.g. ``trading_enabled``).
        default: Default value when neither env nor file sets it.
        needs_restart: True if flipping the flag at runtime cannot take full
            effect until a server restart (e.g. import-time blueprint mounting).
    """
    env_key: str
    json_key: str
    default: bool = False
    needs_restart: bool = False


# json_key -> FeatureFlag. Plugin (optional) flags only; base flags live in core.
_FLAGS: dict[str, FeatureFlag] = {}

# json_key -> bool: whether the flag was ON at boot (so its import-time
# blueprint registration actually happened). Generalises the former
# routes.TRADING_ROUTES_REGISTERED snapshot — features_store uses it to decide
# needs_restart when a needs_restart flag is flipped ON post-boot.
_BOOT_ENABLED: dict[str, bool] = {}


def register_feature_flag(env_key: str, json_key: str, default: bool = False,
                          needs_restart: bool = False, *, replace: bool = False) -> None:
    """Register a plugin feature flag.

    Args:
        env_key / json_key / default / needs_restart: see :class:`FeatureFlag`.
        replace: overwrite an existing registration for ``json_key``.
    """
    if not json_key or not env_key:
        logger.warning('[FeatureRegistry] flag with empty key ignored: env=%r json=%r',
                       env_key, json_key)
        return
    if json_key in _FLAGS and not replace:
        logger.warning('[FeatureRegistry] duplicate flag %r ignored '
                       '(pass replace=True to override)', json_key)
        return
    _FLAGS[json_key] = FeatureFlag(env_key, json_key, bool(default), bool(needs_restart))
    logger.info('[FeatureRegistry] registered feature flag %r (needs_restart=%s)',
                json_key, needs_restart)


def unregister_feature_flag(json_key: str) -> None:
    """Remove a registered flag (mainly for tests)."""
    _FLAGS.pop(json_key, None)


def registered_flags() -> list[FeatureFlag]:
    """Return all registered plugin flags (sorted by json_key)."""
    return [_FLAGS[k] for k in sorted(_FLAGS)]


def get_flag(json_key: str) -> FeatureFlag | None:
    """Return the FeatureFlag for ``json_key`` or None."""
    return _FLAGS.get(json_key)


def mark_boot_enabled(json_key: str, enabled: bool) -> None:
    """Record whether ``json_key`` was enabled at boot (set once at startup)."""
    _BOOT_ENABLED[json_key] = bool(enabled)


def was_boot_enabled(json_key: str) -> bool:
    """Return whether ``json_key`` was enabled at boot (default False)."""
    return _BOOT_ENABLED.get(json_key, False)


def discover_flag_plugins() -> int:
    """Load plugin feature flags from the ``tofu.flags`` entry-point group.

    Returns:
        The number of entry points successfully loaded.
    """
    loaded = 0
    try:
        from importlib.metadata import entry_points
    except Exception as e:  # pragma: no cover
        logger.debug('[FeatureRegistry] importlib.metadata unavailable: %s', e)
        return 0
    try:
        eps = entry_points(group=_ENTRY_POINT_GROUP)
    except TypeError:
        eps = entry_points().get(_ENTRY_POINT_GROUP, [])  # type: ignore[attr-defined]
    except Exception as e:
        logger.debug('[FeatureRegistry] entry_points lookup failed: %s', e)
        return 0
    for ep in eps:
        name = getattr(ep, 'name', '?')
        try:
            register_fn = ep.load()
            register_fn(register_feature_flag)
            loaded += 1
            logger.info('[FeatureRegistry] loaded feature flag(s) from plugin %r', name)
        except Exception as e:
            logger.warning('[FeatureRegistry] plugin %r failed to load: %s',
                           name, e, exc_info=True)
    return loaded


__all__ = [
    'FeatureFlag',
    'register_feature_flag',
    'unregister_feature_flag',
    'registered_flags',
    'get_flag',
    'mark_boot_enabled',
    'was_boot_enabled',
    'discover_flag_plugins',
]
