"""Lightweight task authority for on-demand paper deepening.

Responsibility: own the process-local ``paper-deepen`` task runtime used by
HTTP poll/abort registration. The agent engine imports this authority when a
deepening request starts; ordinary server boot must not import the engine.

Dependencies: the shared task-runtime substrate only.
"""

from lib.agent_core.task_runtime import TaskRuntime


_deepen_runtime = TaskRuntime(
    'paper-deepen', ttl=1800,
    push_channel='paper',
    error_source='routes.paper:deepen',
)


__all__ = ['_deepen_runtime']
