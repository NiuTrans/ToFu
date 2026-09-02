"""Owner-scoped live project presence.

The registry is process-local and TTL-bound because presence represents work
running in this process, not durable project data. Public operations require an
explicit owner and push events are filtered to that owner's connections.
"""

from __future__ import annotations

from lib.presence.registry import (
    announce,
    depart,
    heartbeat,
    mark_idle,
    record_files,
    snapshot,
    start_sweeper,
    stop_sweeper,
    sweep,
)

__all__ = [
    'announce',
    'heartbeat',
    'record_files',
    'mark_idle',
    'depart',
    'sweep',
    'snapshot',
    'start_sweeper',
    'stop_sweeper',
]
