"""Closed recovery decision returned by post-stream analysis.

The analyser still performs policy-specific task/message mutations, but its
control-flow output has one validated shape.  Mapping compatibility keeps old
plugins/tests working while the root orchestrator consumes typed attributes.
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from enum import Enum
from typing import Any

from lib.llm.stream_result import ProviderStreamState


class RecoveryAction(str, Enum):
    BREAK = 'break'
    CONTINUE = 'continue'
    PROGRAM_CONTINUE = 'program_continue'
    PROCEED = 'proceed'


_KEYS = (
    'action',
    'loop_exit_reason',
    'abort_detected_phase',
    'premature_retry_count',
    'last_finish_reason',
    'stream_state',
)


@dataclass(frozen=True, slots=True)
class RecoveryDecision(Mapping[str, Any]):
    """Validated loop-control result for one streamed provider response."""

    action: RecoveryAction
    loop_exit_reason: str | None
    abort_detected_phase: str | None
    premature_retry_count: int
    last_finish_reason: str | None
    stream_state: ProviderStreamState

    def __post_init__(self) -> None:
        if not isinstance(self.action, RecoveryAction):
            raise TypeError('action must be a RecoveryAction')
        if not isinstance(self.stream_state, ProviderStreamState):
            raise TypeError('stream_state must be a ProviderStreamState')
        if self.premature_retry_count < 0:
            raise ValueError('premature_retry_count must be non-negative')
        if self.action is RecoveryAction.BREAK and not self.loop_exit_reason:
            raise ValueError('break recovery decisions require loop_exit_reason')
        if (self.action is not RecoveryAction.BREAK
                and self.loop_exit_reason is not None):
            raise ValueError(
                'only break recovery decisions may carry loop_exit_reason')
        if (self.abort_detected_phase is not None
                and self.action is not RecoveryAction.BREAK):
            raise ValueError('abort_detected_phase requires a break decision')

    @classmethod
    def from_mapping(
        cls,
        value: Mapping[str, Any],
        *,
        stream_state: ProviderStreamState,
    ) -> 'RecoveryDecision':
        try:
            action = RecoveryAction(str(value.get('action') or ''))
        except ValueError as exc:
            raise ValueError(
                f"unknown stream recovery action: {value.get('action')!r}") from exc
        return cls(
            action=action,
            loop_exit_reason=(
                str(value['loop_exit_reason'])
                if value.get('loop_exit_reason') is not None else None),
            abort_detected_phase=(
                str(value['abort_detected_phase'])
                if value.get('abort_detected_phase') is not None else None),
            premature_retry_count=int(
                value.get('premature_retry_count') or 0),
            last_finish_reason=(
                str(value['last_finish_reason'])
                if value.get('last_finish_reason') is not None else None),
            stream_state=stream_state,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            'action': self.action.value,
            'loop_exit_reason': self.loop_exit_reason,
            'abort_detected_phase': self.abort_detected_phase,
            'premature_retry_count': self.premature_retry_count,
            'last_finish_reason': self.last_finish_reason,
            'stream_state': self.stream_state.value,
        }

    def __getitem__(self, key: str) -> Any:
        return self.to_dict()[key]

    def __iter__(self) -> Iterator[str]:
        return iter(_KEYS)

    def __len__(self) -> int:
        return len(_KEYS)


__all__ = ['RecoveryAction', 'RecoveryDecision']
