"""Canonical storage vocabulary for durable task-event streams.

This module is dependency-free so producers, the Sidecar schema operations,
maintenance, and read projections all consume the same machine-readable
classification instead of duplicating retention policy.
"""

from __future__ import annotations


TASK_STREAM_KIND = 'task'
PROJECT_BRAIN_STREAM_KIND = 'project_brain'
TASK_EVENT_STREAMING_RETENTION_MS = 6 * 60 * 60 * 1000
TASK_EVENT_STRUCTURAL_RETENTION_MS = 30 * 24 * 60 * 60 * 1000

STREAM_KINDS = frozenset({
    TASK_STREAM_KIND,
    PROJECT_BRAIN_STREAM_KIND,
})

STRUCTURAL_EVENT_TYPES = frozenset({
    'messages_snapshot',
    'tool_wire_projection',
    'round_usage',
    'round_start',
    'round_end',
    'flow_iteration',
})

TERMINAL_EVENT_TYPES = frozenset({
    'done',
    'error',
    'aborted',
    'interrupted',
})


__all__ = [
    'PROJECT_BRAIN_STREAM_KIND',
    'STREAM_KINDS',
    'STRUCTURAL_EVENT_TYPES',
    'TASK_EVENT_STREAMING_RETENTION_MS',
    'TASK_EVENT_STRUCTURAL_RETENTION_MS',
    'TASK_STREAM_KIND',
    'TERMINAL_EVENT_TYPES',
]
