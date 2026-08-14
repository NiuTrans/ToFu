"""Shared-context feedback and convergence state for orchestration loops.

The feedback channel owns the lifecycle that connects verifier turns to the
next shared-context producer: bounded prior-attempt memory, pending reviewer
feedback/directives, repeating-feedback detection and the virtual-user
progress ledger.  It deliberately has no graph-walk or agent dependency.
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from typing import Any

from lib.agent_verdict import (
    AUTOPILOT_STUCK_WINDOW,
    autopilot_progress_window,
    detect_diminishing_returns,
    detect_stuck,
    parse_progress,
)

CARRY_ATTEMPT_CHARS = 1800
CARRY_FEEDBACK_CHARS = 1800
STUCK_JACCARD = 0.60


class OrchestrationFeedbackState:
    """Thread-safe feedback channel and per-loop convergence ledger."""

    def __init__(
        self,
        *,
        lock: Any | None = None,
        attempt_chars: int = CARRY_ATTEMPT_CHARS,
        feedback_chars: int = CARRY_FEEDBACK_CHARS,
    ):
        self._lock = lock or threading.Lock()
        self._attempt_chars = max(1, int(attempt_chars))
        self._feedback_chars = max(1, int(feedback_chars))
        self._node_memory: dict[str, str] = {}
        self._pending_feedback = ''
        self._pending_directive = ''
        self._history: list[str] = []
        self._vu_progress: list[dict] = []

    def reset_loop(self) -> None:
        """Reset loop-local state while retaining per-node attempt memory."""
        with self._lock:
            self._pending_feedback = ''
            self._pending_directive = ''
            self._history = []
            self._vu_progress = []

    def compose_shared_context(self, node_id: str, upstream: str) -> str:
        """Compose bounded memory and pending control feedback for a producer."""
        with self._lock:
            prior = self._node_memory.get(node_id, '')
            feedback = self._pending_feedback
            directive = self._pending_directive

        parts = [upstream] if upstream else []
        if prior:
            parts.append(
                '## Your previous attempt\n' + prior[-self._attempt_chars:]
            )
        if feedback:
            parts.append(
                '## Reviewer feedback to address\n'
                + feedback[-self._feedback_chars:]
            )
        if directive:
            parts.append('## ⚠️ Directive\n' + directive)
        return '\n\n'.join(parts)

    def complete_role(
        self,
        node_id: str,
        output: str,
        *,
        shared: bool,
        verifier: bool,
    ) -> None:
        """Commit a role turn and advance the single-valued feedback channel."""
        with self._lock:
            if shared:
                self._node_memory[node_id] = output
                self._pending_feedback = ''
                self._pending_directive = ''
            if verifier:
                self._pending_feedback = output

    def set_directive(self, directive: str) -> None:
        with self._lock:
            self._pending_directive = directive or ''

    def append_verifier_feedback(self, output: str) -> None:
        with self._lock:
            self._history.append(output or '')

    def record_virtual_user_progress(
        self,
        verifier_output: str,
        producer_snapshot: dict,
        *,
        progress_parser: Callable[[str], tuple] = parse_progress,
    ) -> dict:
        """Record one VU hard-progress signal and deterministic churn targets."""
        resolved, _remaining = progress_parser(verifier_output or '')
        with self._lock:
            previous = next(
                (
                    entry.get('cum_resolved')
                    for entry in reversed(self._vu_progress)
                    if entry.get('cum_resolved') is not None
                ),
                None,
            )
            if resolved is None:
                delta, cumulative = None, previous
            else:
                delta = resolved - previous if previous is not None else resolved
                delta = max(0, delta)
                cumulative = resolved
            targets = sorted({
                str(name)
                for name in (producer_snapshot.get('names') or [])
                if name
            })
            entry = {
                'resolved_delta': delta,
                'cum_resolved': cumulative,
                'targets': targets,
            }
            self._vu_progress.append(entry)
            return dict(entry)

    def detects_stuck(self, *, verifier_role: str = '') -> bool:
        """Apply the verifier-specific repetition window to current history."""
        window = AUTOPILOT_STUCK_WINDOW if verifier_role == 'virtual_user' else 2
        with self._lock:
            history = list(self._history)
        return detect_stuck(history, threshold=STUCK_JACCARD, window=window)

    def no_progress_window(self) -> int:
        """Return the active VU diminishing-returns window, or zero."""
        window = autopilot_progress_window()
        if not window:
            return 0
        with self._lock:
            progress = [dict(entry) for entry in self._vu_progress]
        return window if detect_diminishing_returns(progress, window=window) else 0

    def node_memory_snapshot(self) -> dict[str, str]:
        with self._lock:
            return dict(self._node_memory)

    def replace_node_memory(self, memory: dict[str, str]) -> None:
        with self._lock:
            self._node_memory = dict(memory or {})

    def pending_feedback(self) -> str:
        with self._lock:
            return self._pending_feedback

    def replace_pending_feedback(self, feedback: str) -> None:
        with self._lock:
            self._pending_feedback = feedback or ''

    def pending_directive(self) -> str:
        with self._lock:
            return self._pending_directive

    def replace_pending_directive(self, directive: str) -> None:
        self.set_directive(directive)

    def history_snapshot(self) -> list[str]:
        with self._lock:
            return list(self._history)

    def replace_history(self, history: list[str]) -> None:
        with self._lock:
            self._history = list(history or [])

    def vu_progress_snapshot(self) -> list[dict]:
        with self._lock:
            return [dict(entry) for entry in self._vu_progress]

    def replace_vu_progress(self, progress: list[dict]) -> None:
        with self._lock:
            self._vu_progress = [dict(entry) for entry in progress or []]


__all__ = [
    'CARRY_ATTEMPT_CHARS',
    'CARRY_FEEDBACK_CHARS',
    'STUCK_JACCARD',
    'OrchestrationFeedbackState',
]
