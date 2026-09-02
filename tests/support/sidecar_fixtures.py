"""Scope helpers for opt-in real-Sidecar pytest plugins.

Pytest registers a module named by ``pytest_plugins`` as a process-wide
plugin. An ``autouse`` fixture from that plugin is therefore considered for
every subsequently collected test module, not only the module that declared
it. Real-Sidecar fixtures must check the current module's declaration before
starting a subprocess or creating a database.
"""

from __future__ import annotations


def module_declares_plugin(request, plugin_name: str) -> bool:
    """Return whether the current test module explicitly opted into a plugin."""
    declared = getattr(request.module, 'pytest_plugins', ())
    if isinstance(declared, str):
        declared = (declared,)
    try:
        return plugin_name in declared
    except TypeError:
        return False


__all__ = ['module_declares_plugin']
