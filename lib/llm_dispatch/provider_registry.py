"""lib/llm_dispatch/provider_registry.py — Pluggable LLM body-shape dialects.

This is the **provider-side mirror** of ``lib/tools/registry.py``.  Where the
tool registry lets a third-party package contribute *tools*, this registry lets
one contribute a *body dialect* — the engine-specific shape of the
thinking/temperature parameters in a ``/chat/completions`` request body.

Why this exists
---------------
A ``Slot``'s ``thinking_format`` string selects how
``lib/llm/body.py::build_body`` and
``lib/llm_dispatch/api.py::_readjust_thinking_params`` translate
"thinking on/off" into wire parameters.  Historically, adding a new engine
dialect meant editing THREE core spots: the ``THINKING_FORMATS`` frozenset,
plus a branch in each of those two hot-path functions.

This registry adds a fourth option that requires editing NONE of them: ship a
:class:`BodyDialect` from an external package via the ``tofu.providers``
entry-point group.  The built-in dialects (``none``, ``chat_template_kwargs``,
``enable_thinking``, ``thinking_type``) and all the model-name auto-detect
heuristics stay exactly where they are — this layer is consulted FIRST, only
for keys it actually owns, so existing behaviour is byte-for-byte unchanged.

Design contract
---------------
1. **Custom-only.**  The registry holds ONLY plugin dialects.  Built-in keys
   are intentionally NOT registered here — they remain handled by the existing
   ladders in core.  :func:`get_dialect` returns ``None`` for a built-in key,
   so core falls through to its own branch.
2. **Two hooks, symmetric.**  A dialect implements ``apply_build`` (fresh body)
   and ``apply_readjust`` (a pre-built body re-targeted to a new model after a
   slot swap).  Both mutate ``body`` in place.  ``apply_readjust`` defaults to
   ``apply_build`` semantics when omitted.
3. **Validation stays strict.**  :func:`is_valid_thinking_format` is the single
   predicate the Slot constructor + BYO-provider validator consult; it accepts
   a value iff it is a built-in format OR a registered plugin dialect.  Typos
   still raise (the incident that motivated the original strict check).
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field

from lib.log import get_logger

logger = get_logger(__name__)

# Build/readjust hooks mutate the request ``body`` dict in place.  Kwargs carry
# everything a dialect could need; a dialect ignores what it doesn't use.
DialectHook = Callable[..., None]


@dataclass(frozen=True)
class BodyDialect:
    """A pluggable request-body dialect for a custom inference engine.

    Parameters
    ----------
    key:
        The ``thinking_format`` string that selects this dialect (e.g.
        ``'my_engine_v1'``).  MUST NOT collide with a built-in format.
    apply_build:
        ``apply_build(body, *, thinking_enabled, temperature, model, effort)``
        — mutate a freshly-built body to carry this engine's thinking params.
    apply_readjust:
        ``apply_readjust(body, *, is_enabled, model, effort)`` — re-apply this
        dialect to a body that was built for a different model and is being
        dispatched to a slot speaking this dialect.  Defaults to a thin wrapper
        over :attr:`apply_build` when not supplied.
    description:
        Human-readable note for introspection / docs.
    """

    key: str
    apply_build: DialectHook
    apply_readjust: DialectHook | None = None
    description: str = ''
    _meta: dict = field(default_factory=dict, compare=False)


# ── Module-level registry (plugin dialects only) ──
_DIALECTS: dict[str, BodyDialect] = {}

# Built-in formats owned by the core ladders — NOT stored in _DIALECTS.
# Kept here so registration can reject collisions and is_valid_* can accept
# them.  Mirror of lib.llm_dispatch.slot.THINKING_FORMATS (imported lazily to
# avoid a circular import at module load).
_BUILTIN_FORMATS = frozenset({
    '', 'enable_thinking', 'thinking_type', 'chat_template_kwargs', 'none',
})


def register_dialect(dialect: BodyDialect, *, replace: bool = False) -> None:
    """Register a custom :class:`BodyDialect`.

    Args:
        dialect: The dialect to add.
        replace: If ``True``, overwrite an existing plugin dialect with the same
            key.  Built-in keys can never be overridden.
    """
    key = dialect.key
    if key in _BUILTIN_FORMATS:
        logger.warning('[ProviderRegistry] dialect key=%r collides with a '
                       'built-in format — ignored', key)
        return
    if not key or not isinstance(key, str):
        logger.warning('[ProviderRegistry] dialect with empty/invalid key '
                       'ignored: %r', key)
        return
    if key in _DIALECTS and not replace:
        logger.warning('[ProviderRegistry] duplicate dialect key=%r ignored '
                       '(pass replace=True to override)', key)
        return
    _DIALECTS[key] = dialect
    logger.info('[ProviderRegistry] registered body dialect key=%r', key)


def get_dialect(key: str) -> BodyDialect | None:
    """Return the plugin dialect for *key*, or ``None`` (incl. for built-ins)."""
    if not key:
        return None
    return _DIALECTS.get(key)


def all_dialects() -> list[BodyDialect]:
    """Return all registered plugin dialects."""
    return list(_DIALECTS.values())


def dialect_keys() -> frozenset[str]:
    """Return the set of registered plugin dialect keys."""
    return frozenset(_DIALECTS)


def is_valid_thinking_format(value: str) -> bool:
    """True iff *value* is a built-in format OR a registered plugin dialect.

    Single predicate for the Slot constructor + BYO-provider validator.
    """
    return value in _BUILTIN_FORMATS or value in _DIALECTS


# ══════════════════════════════════════════════════════════
#  Plugin discovery — external packages contribute via entry points
# ══════════════════════════════════════════════════════════

def discover_provider_plugins() -> int:
    """Load custom dialects from the ``tofu.providers`` entry-point group.

    A plugin package declares in its ``pyproject.toml``::

        [project.entry-points."tofu.providers"]
        my_engine = "my_pkg.dialect:register"

    where ``register`` receives :func:`register_dialect` and uses it to add one
    or more :class:`BodyDialect` objects.  Failures in any single plugin are
    logged and skipped.

    Returns:
        The number of entry points successfully loaded.
    """
    loaded = 0
    try:
        from importlib.metadata import entry_points
    except Exception as e:  # pragma: no cover
        logger.debug('[ProviderRegistry] importlib.metadata unavailable: %s', e)
        return 0
    try:
        eps = entry_points(group='tofu.providers')
    except TypeError:
        eps = entry_points().get('tofu.providers', [])  # type: ignore[attr-defined]
    except Exception as e:
        logger.debug('[ProviderRegistry] entry_points lookup failed: %s', e)
        return 0
    for ep in eps:
        try:
            register_fn = ep.load()
            register_fn(register_dialect)
            loaded += 1
            logger.info('[ProviderRegistry] loaded provider dialect(s) from %s',
                        ep.name)
        except Exception as e:
            logger.warning('[ProviderRegistry] plugin %s failed to load: %s',
                           getattr(ep, 'name', '?'), e, exc_info=True)
    return loaded


# Discover plugins at import time.  Built-in dialects are intentionally NOT
# registered — core ladders own them.
discover_provider_plugins()


__all__ = [
    'BodyDialect',
    'register_dialect',
    'get_dialect',
    'all_dialects',
    'dialect_keys',
    'is_valid_thinking_format',
    'discover_provider_plugins',
]
