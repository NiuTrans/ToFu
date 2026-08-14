"""Framework-neutral result model for orchestration mutations."""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Final

from lib.orchestration.mutation_payload_fields import (
    mutation_payload_field_names,
)
from lib.orchestration.wire_formats import MUTATION_FORMAT


MUTATION_ACCEPTED: Final = 'accepted'
MUTATION_NOT_FOUND: Final = 'not_found'
MUTATION_TERMINAL: Final = 'terminal'
MUTATION_ACTIVE: Final = 'active'
MUTATION_CONFLICT: Final = 'conflict'
MUTATION_PERSISTENCE_FAILED: Final = 'persistence_failed'
MUTATION_TRANSPORT_FAILED: Final = 'transport_failed'

MUTATION_ACTION_ABORT_RUN: Final = 'abort_run'
MUTATION_ACTION_DELETE_RUN: Final = 'delete_run'
MUTATION_ACTION_APPROVE_GATE: Final = 'approve_gate'
MUTATION_ACTION_INPUT_GATE: Final = 'input_gate'
MUTATION_ACTION_TRANSITION_RUN: Final = 'transition_run'

MUTATION_LEGACY_RUN_ID_FIELD: Final = 'run_id'
MUTATION_LEGACY_GATE_ID_FIELD: Final = 'requestId'
MUTATION_LEGACY_RUN_STATUS_FIELD: Final = 'run_status'
MUTATION_LEGACY_STATUS_FIELD: Final = 'status'

MUTATION_RETRYABLE_REASONS: Final = frozenset({
    MUTATION_PERSISTENCE_FAILED,
})


@dataclass(frozen=True)
class OrchestrationMutationResult:
    """Canonical result for one state-changing operation."""

    ok: bool
    reason: str = ''
    run_status: str = ''
    action: str = ''
    target_id: str = ''

    @property
    def canonical_reason(self) -> str:
        return MUTATION_ACCEPTED if self.ok else (
            self.reason or MUTATION_CONFLICT)

    @property
    def retryable(self) -> bool:
        return (
            not self.ok
            and self.canonical_reason in MUTATION_RETRYABLE_REASONS
        )

    @property
    def reconcile_required(self) -> bool:
        return not self.ok

    @property
    def target_exists(self) -> bool | None:
        reason = self.canonical_reason
        if self.action in {
            MUTATION_ACTION_APPROVE_GATE,
            MUTATION_ACTION_INPUT_GATE,
        }:
            if self.ok or reason == MUTATION_NOT_FOUND:
                return False
            return None
        if self.action == MUTATION_ACTION_DELETE_RUN:
            if self.ok or reason == MUTATION_NOT_FOUND:
                return False
            if reason == MUTATION_ACTIVE:
                return True
            return None
        if self.action in {
            MUTATION_ACTION_ABORT_RUN,
            MUTATION_ACTION_TRANSITION_RUN,
        }:
            return False if reason == MUTATION_NOT_FOUND else True
        return None

    @property
    def resource_terminal(self) -> bool | None:
        if not self.run_status:
            return None
        from lib.orchestration.run_status import (
            is_run_status,
            is_terminal_run_status,
        )
        if not is_run_status(self.run_status):
            return None
        return is_terminal_run_status(self.run_status)

    def scoped(
        self,
        action: str,
        target_id: str = '',
    ) -> OrchestrationMutationResult:
        return replace(
            self,
            action=str(action or self.action),
            target_id=str(target_id or self.target_id),
        )

    def payload(self) -> dict:
        fields = mutation_payload_field_names()
        return {
            fields['format']: MUTATION_FORMAT,
            fields['ok']: bool(self.ok),
            fields['action']: str(self.action or ''),
            fields['reason']: self.canonical_reason,
            fields['targetId']: str(self.target_id or ''),
            fields['resourceStatus']: str(self.run_status or ''),
            fields['resourceTerminal']: self.resource_terminal,
            fields['targetExists']: self.target_exists,
            fields['retryable']: self.retryable,
            fields['reconcileRequired']: self.reconcile_required,
        }


RunMutationResult = OrchestrationMutationResult


__all__ = [
    'MUTATION_ACCEPTED',
    'MUTATION_ACTION_ABORT_RUN',
    'MUTATION_ACTION_APPROVE_GATE',
    'MUTATION_ACTION_DELETE_RUN',
    'MUTATION_ACTION_INPUT_GATE',
    'MUTATION_ACTION_TRANSITION_RUN',
    'MUTATION_ACTIVE',
    'MUTATION_CONFLICT',
    'MUTATION_FORMAT',
    'MUTATION_LEGACY_GATE_ID_FIELD',
    'MUTATION_LEGACY_RUN_ID_FIELD',
    'MUTATION_LEGACY_RUN_STATUS_FIELD',
    'MUTATION_LEGACY_STATUS_FIELD',
    'MUTATION_NOT_FOUND',
    'MUTATION_PERSISTENCE_FAILED',
    'MUTATION_RETRYABLE_REASONS',
    'MUTATION_TERMINAL',
    'MUTATION_TRANSPORT_FAILED',
    'OrchestrationMutationResult',
    'RunMutationResult',
]
