"""Retain declared direct-call schemas from the live task catalog.

This is provider exposure policy only. Registry builders and the dispatch
pipeline still own availability, permissions and completion evidence.
"""
from __future__ import annotations

from typing import Any


def _name(tool: Any) -> str:
    if not isinstance(tool, dict):
        return ''
    function = tool.get('function')
    return str((function.get('name') if isinstance(function, dict) else '')
               or tool.get('name') or '')


def declared_direct_names(declared: Any) -> set[str]:
    """Read a registry sidecar; malformed metadata grants no projection."""
    if not isinstance(declared, (list, tuple, set, frozenset)):
        return set()
    return {name for name in declared if isinstance(name, str) and name}


def live_direct_names(authority_catalog: list[dict[str, Any]],
                      declared: Any) -> set[str]:
    return declared_direct_names(declared) & {
        _name(tool) for tool in authority_catalog if _name(tool)}


def preserve_direct_wire_tools(tools: list[dict[str, Any]] | None, *,
                               authority_catalog: list[dict[str, Any]],
                               declared: Any) -> list[dict[str, Any]]:
    """Restore exact live schemas, remove revoked copies and deduplicate.

    Keep existing positions and append only missing declarations in catalog
    order. Never use a stale provider schema to restore execution authority.
    """
    protected = declared_direct_names(declared)
    current = {_name(tool): tool for tool in authority_catalog
               if _name(tool) in protected}
    visible = []
    seen = set()
    for tool in tools or ():
        name = _name(tool)
        if name not in protected:
            visible.append(tool)
        elif name in current and name not in seen:
            visible.append(current[name])
            seen.add(name)
    visible.extend(tool for name, tool in current.items() if name not in seen)
    return visible
