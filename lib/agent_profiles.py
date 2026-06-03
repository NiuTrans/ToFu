"""lib/agent_profiles.py — Compatibility shim.

The implementation moved to :mod:`lib.agent_core.profiles` as part of the
agent-base relocation (2026-06).  This shim preserves the historical import
path ``from lib.agent_profiles import apply_profile, ...`` so existing call
sites keep working unchanged.

Prefer importing from the new home in new code::

    from lib.agent_core.profiles import apply_profile, resolve_profile_name
    # or via the facade:
    from lib.agent_core import apply_profile
"""

from __future__ import annotations

from lib.agent_core.profiles import (
    apply_profile,
    get_profile,
    list_profiles,
    resolve_profile_name,
)

__all__ = [
    'list_profiles',
    'get_profile',
    'apply_profile',
    'resolve_profile_name',
]
