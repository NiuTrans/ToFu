"""Bounded per-node trace recorder for orchestration runs.

The recorder owns trace sequencing, truncation and durable ``step_trace``
projection. It has no graph-walk, agent, verdict or Typed-I/O dependency.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from typing import Any

from lib.log import get_logger
from lib.orchestration.contract_schema import contract_snapshot_schema
from lib.orchestration.wire_formats import TRACE_FORMAT as TRACE_CONTRACT_FORMAT

logger = get_logger(__name__)

TRACE_INPUT_CHARS = 8000
TRACE_OUTPUT_CHARS = 16000
TRACE_ERROR_CHARS = 4000
TRACE_HISTORY_ENTRIES = 12
TRACE_STATUS_MAP = {
    'running': 'running',
    'completed': 'done',
    'failed': 'error',
    'done': 'done',
    'error': 'error',
}
TRACE_ACTIVITY_FIELDS = {
    'stateChanging': 'state_changing',
    'exploratory': 'exploratory',
    'stateChangingTools': 'state_changing_tools',
}


def trace_activity_snapshot(
    *,
    state_changing: object = 0,
    exploratory: object = 0,
    state_changing_tools: object = None,
) -> dict:
    """Return one normalized activity payload for trace and completion events."""
    def count(value: object) -> int:
        try:
            return max(0, int(value))
        except (TypeError, ValueError, OverflowError) as exc:
            logger.debug('[Trace] invalid activity count %r: %s', value, exc)
            return 0

    tools = []
    if isinstance(state_changing_tools, (list, tuple)):
        for value in state_changing_tools:
            name = str(value or '').strip()
            if name:
                tools.append(name)
    return {
        TRACE_ACTIVITY_FIELDS['stateChanging']: count(state_changing),
        TRACE_ACTIVITY_FIELDS['exploratory']: count(exploratory),
        TRACE_ACTIVITY_FIELDS['stateChangingTools']: tools,
    }


def trace_contract() -> dict:
    """Describe the bounded trace text projected to every run surface."""
    limits = {
        'brief': TRACE_INPUT_CHARS,
        'input': TRACE_INPUT_CHARS,
        'output': TRACE_OUTPUT_CHARS,
        'thinking': TRACE_OUTPUT_CHARS,
        'error': TRACE_ERROR_CHARS,
    }
    return {
        'format': TRACE_CONTRACT_FORMAT,
        'historyLimit': TRACE_HISTORY_ENTRIES,
        'statusMap': dict(TRACE_STATUS_MAP),
        'activityFields': dict(TRACE_ACTIVITY_FIELDS),
        'textLimits': limits,
        'truncationFlags': {
            field: f'{field}_truncated' for field in limits
        },
    }


def trace_contract_schema() -> dict:
    """Describe trace bounds and status projection from the live contract."""
    return contract_snapshot_schema(trace_contract())


class OrchestrationTraceRecorder:
    """Thread-safe, bounded trace store for one executor instance."""

    def __init__(
        self,
        *,
        emit: Callable[[dict], None],
        lock: Any | None = None,
        input_chars: int = TRACE_INPUT_CHARS,
        output_chars: int = TRACE_OUTPUT_CHARS,
        error_chars: int = TRACE_ERROR_CHARS,
    ):
        self._emit = emit
        self._lock = lock or threading.Lock()
        self._input_chars = max(1, int(input_chars))
        self._output_chars = max(1, int(output_chars))
        self._error_chars = max(1, int(error_chars))
        self._entries: list[dict] = []
        self._sequence = 0

    def capture(
        self,
        node: dict,
        *,
        iteration: int,
        brief: str,
        input_context: str,
        output: str,
        status: str,
        error: str,
        elapsed: float,
        emits: str,
        isolation: str,
        state_changing: int = 0,
        exploratory: int = 0,
        state_changing_tools: list | None = None,
        subflow: bool = False,
        thinking: str = '',
    ) -> None:
        """Capture and emit one self-contained trace entry; never raise."""
        try:
            input_text = input_context or ''
            output_text = output or ''
            thinking_text = thinking or ''
            brief_text = brief or ''
            error_text = error or ''
            entry = {
                'seq': 0,
                'node_id': node.get('id'),
                'role': node.get('role') or '',
                'name': node.get('name') or '',
                'kind': node.get('type') or '',
                'iteration': iteration,
                'emits': emits,
                'isolation': isolation,
                'subflow': bool(subflow),
                'brief': brief_text[:self._input_chars],
                'brief_truncated': len(brief_text) > self._input_chars,
                'input': input_text[:self._input_chars],
                'input_truncated': len(input_text) > self._input_chars,
                'output': output_text[:self._output_chars],
                'output_truncated': len(output_text) > self._output_chars,
                'thinking': thinking_text[:self._output_chars],
                'thinking_truncated': len(thinking_text) > self._output_chars,
                'status': status,
                'error': error_text[:self._error_chars],
                'error_truncated': len(error_text) > self._error_chars,
                'elapsed': round(elapsed, 2),
                **trace_activity_snapshot(
                    state_changing=state_changing,
                    exploratory=exploratory,
                    state_changing_tools=state_changing_tools,
                ),
                'ts': _now_iso(),
            }
            with self._lock:
                self._sequence += 1
                entry['seq'] = self._sequence
                self._entries.append(entry)
            self._emit({'type': 'step_trace', **entry})
        except Exception as exc:
            logger.debug(
                '[FlowTrace] capture failed for %s: %s',
                node.get('id'),
                exc,
            )

    def snapshot(self) -> list[dict]:
        """Return the live trace list as a detached outer container."""
        with self._lock:
            return list(self._entries)


def _now_iso() -> str:
    return time.strftime('%Y-%m-%dT%H:%M:%S')


__all__ = [
    'TRACE_CONTRACT_FORMAT',
    'TRACE_ERROR_CHARS',
    'TRACE_HISTORY_ENTRIES',
    'TRACE_INPUT_CHARS',
    'TRACE_OUTPUT_CHARS',
    'TRACE_ACTIVITY_FIELDS',
    'TRACE_STATUS_MAP',
    'OrchestrationTraceRecorder',
    'trace_activity_snapshot', 'trace_contract', 'trace_contract_schema',
]
