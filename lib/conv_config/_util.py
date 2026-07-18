"""lib/conv_config/_util.py — shared merge helpers.

Small, dependency-free primitives used by the config/settings resolvers
(:mod:`lib.conv_config._resolve`). Kept here so multiple submodules can
share them without an import cycle.
"""

from __future__ import annotations

from typing import Any

from lib.log import get_logger

logger = get_logger(__name__)


def _coerce_bool(v: Any, default: bool = False) -> bool:
    """JS-compatible truthiness check.

    The JS impl uses ``!!conv.X`` everywhere; in Python ``bool(0) == False``,
    ``bool('') == False``, ``bool(None) == False``, which matches.
    """
    if v is None:
        return default
    return bool(v)


def _first_defined(*candidates):
    """Return the first non-``None`` candidate, falling back to ``None``."""
    for c in candidates:
        if c is not None:
            return c
    return None


def _pick(active: Any, inactive: Any, *, is_active: bool):
    """Pick ``active`` when current conv is active, else ``inactive``."""
    return active if is_active else inactive
