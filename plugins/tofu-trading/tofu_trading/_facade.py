"""Package-local helpers for composing public module facades.

This plugin must not import the host's private ``lib._pkg_utils`` module.
Only the tiny ``build_facade`` behavior is needed here, so the plugin owns it
and remains insulated from host-internal package refactors.
"""

from __future__ import annotations

from types import ModuleType


def build_facade(public_names: list[str], *modules: ModuleType) -> None:
    """Append each module's explicit exports to ``public_names`` in order."""
    for module in modules:
        exports = getattr(module, '__all__', None)
        if exports:
            public_names.extend(exports)


__all__ = ['build_facade']
