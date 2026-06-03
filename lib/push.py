"""lib/push.py — Compatibility shim.

The implementation moved to :mod:`lib.agent_core.push` as part of the
agent-base relocation (2026-06).  This shim preserves the historical import
path ``from lib.push import hub, push_event, ...`` so existing call sites keep
working unchanged.

Because everything is re-exported from the single new module, the ``hub``
singleton identity is preserved — ``lib.push.hub is lib.agent_core.push.hub``.

Prefer importing from the new home in new code::

    from lib.agent_core.push import hub, push_event
    # or via the facade:
    from lib.agent_core import hub, push_event
"""

from __future__ import annotations

from lib.agent_core.push import (
    PushClient,
    PushHub,
    broadcast,
    hub,
    push_event,
)

__all__ = ['PushHub', 'PushClient', 'hub', 'push_event', 'broadcast']
