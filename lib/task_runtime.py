"""lib/task_runtime.py — Compatibility shim.

The implementation moved to :mod:`lib.agent_core.task_runtime` as part of the
agent-base relocation (2026-06).  This shim preserves the historical import
path ``from lib.task_runtime import TaskRuntime`` so existing call sites keep
working unchanged.

Prefer importing from the new home in new code::

    from lib.agent_core.task_runtime import TaskRuntime
    # or via the facade:
    from lib.agent_core import TaskRuntime
"""

from __future__ import annotations

from lib.agent_core.task_runtime import TaskRuntime

__all__ = ['TaskRuntime']
