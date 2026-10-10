"""Schema for the bounded parent/Sidecar startup control channel.

Responsibility: define and validate progress envelopes emitted before the
final ``storage.ready`` or ``storage.error`` envelope.  The channel carries
only phase names and aggregate work counters; database paths, credentials,
and authority contents never cross this boundary.
"""

from __future__ import annotations

from dataclasses import dataclass
import json
import re
from typing import Any, Mapping, Protocol

from lib.storage.protocol import PROTOCOL_VERSION


STARTUP_PROGRESS_TYPE = 'storage.startup_progress'
STARTUP_READY_TYPE = 'storage.ready'
STARTUP_ERROR_TYPE = 'storage.error'
MAX_STARTUP_CONTROL_LINE_CHARS = 4096
_PHASE_PATTERN = re.compile(r'^[a-z][a-z0-9_.-]{0,95}$')


class StartupProgressCallback(Protocol):
    """Explicit Sidecar callback for one bounded startup work observation."""

    def __call__(
        self,
        phase: str,
        completed_bytes: int,
        total_bytes: int,
        *,
        heartbeat: bool = False,
    ) -> None: ...


@dataclass(frozen=True)
class StartupProgress:
    phase: str
    completed_bytes: int
    total_bytes: int
    heartbeat: bool


def _validated_progress_fields(
    phase: Any,
    completed_bytes: Any,
    total_bytes: Any,
    heartbeat: Any,
) -> StartupProgress:
    normalized_phase = str(phase or '')
    if not _PHASE_PATTERN.fullmatch(normalized_phase):
        raise ValueError('invalid storage startup progress phase')
    if (isinstance(completed_bytes, bool)
            or not isinstance(completed_bytes, int)
            or isinstance(total_bytes, bool)
            or not isinstance(total_bytes, int)):
        raise ValueError('invalid storage startup progress counters')
    if total_bytes < 0 or not 0 <= completed_bytes <= total_bytes:
        raise ValueError('invalid storage startup progress range')
    if not isinstance(heartbeat, bool):
        raise ValueError('invalid storage startup heartbeat flag')
    return StartupProgress(
        phase=normalized_phase,
        completed_bytes=completed_bytes,
        total_bytes=total_bytes,
        heartbeat=heartbeat,
    )


def encode_startup_progress(
    phase: str,
    completed_bytes: int,
    total_bytes: int,
    *,
    heartbeat: bool = False,
) -> str:
    """Return one newline-free, size-bounded progress control envelope."""
    progress = _validated_progress_fields(
        phase, completed_bytes, total_bytes, heartbeat)
    encoded = json.dumps({
        'type': STARTUP_PROGRESS_TYPE,
        'protocol': PROTOCOL_VERSION,
        'phase': progress.phase,
        'completed_bytes': progress.completed_bytes,
        'total_bytes': progress.total_bytes,
        'heartbeat': progress.heartbeat,
    }, separators=(',', ':'))
    if len(encoded) > MAX_STARTUP_CONTROL_LINE_CHARS:
        raise ValueError('storage startup progress envelope is too large')
    return encoded


def parse_startup_progress(message: Mapping[str, Any]) -> StartupProgress | None:
    """Validate a progress envelope; return ``None`` for another type."""
    if message.get('type') != STARTUP_PROGRESS_TYPE:
        return None
    if message.get('protocol') != PROTOCOL_VERSION:
        raise ValueError('storage startup progress protocol mismatch')
    return _validated_progress_fields(
        message.get('phase'),
        message.get('completed_bytes'),
        message.get('total_bytes'),
        message.get('heartbeat'),
    )


__all__ = [
    'MAX_STARTUP_CONTROL_LINE_CHARS',
    'STARTUP_ERROR_TYPE',
    'STARTUP_PROGRESS_TYPE',
    'STARTUP_READY_TYPE',
    'StartupProgress',
    'StartupProgressCallback',
    'encode_startup_progress',
    'parse_startup_progress',
]
