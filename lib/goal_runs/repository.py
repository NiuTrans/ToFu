"""Owner-bound Sidecar repository for durable GoalRun semantics."""

from __future__ import annotations

import uuid
from collections.abc import Callable, Mapping
from typing import Any, Protocol

from lib.identity import require_user_id
from lib.storage.client import StorageClient
from lib.storage.errors import StorageError


class GoalRunRepositoryError(RuntimeError):
    """The durable GoalRun authority could not accept an operation."""


class GoalRunRepositoryPort(Protocol):
    def start(
        self,
        run_id: str,
        *,
        conversation_id: str,
        objective: str,
        definition: Mapping[str, Any],
        policy: Mapping[str, Any],
    ) -> dict: ...

    def transition(
        self,
        run_id: str,
        *,
        status: str,
        reason: str,
        final: str = '',
        outcome: Mapping[str, Any] | None = None,
    ) -> dict: ...

    def latest_for_conversation(self, conversation_id: str) -> dict | None: ...


class SidecarGoalRunRepository:
    """Translate GoalRun use cases into semantic Sidecar operations."""

    def __init__(
        self,
        owner_user_id: int,
        *,
        tenant_id: str | None = None,
        client: Callable[..., StorageClient] | None = None,
    ) -> None:
        self.owner_user_id = require_user_id(
            owner_user_id, context='goal run owner')
        self.tenant_id = str(tenant_id or '').strip()
        if client is None:
            from lib.storage import get_storage_client
            client = get_storage_client
        self._client = client

    def _boundary(self) -> dict[str, object]:
        return {
            'user_id': self.owner_user_id,
            'tenant_id': self.tenant_id,
        }

    @staticmethod
    def _command_id(operation: str, payload: Mapping[str, Any]) -> str:
        del payload
        return f'goal:{operation}:{uuid.uuid4().hex}'

    def _command(
        self,
        operation: str,
        payload: dict,
        *,
        retry_conflict: bool = False,
    ) -> dict:
        command_id = self._command_id(operation, payload)
        for attempt in range(2):
            try:
                result = self._client(write=True).command(
                    operation, payload, command_id)
                if not isinstance(result, dict):
                    raise GoalRunRepositoryError(
                        f'{operation} returned a non-object result')
                return result
            except StorageError as error:
                retry = (
                    attempt == 0
                    and (error.retryable
                         or (retry_conflict and error.code == 'database_conflict'))
                )
                if retry:
                    continue
                raise GoalRunRepositoryError(
                    f'{operation} failed: {error.code}') from error
        raise GoalRunRepositoryError(f'{operation} retry was exhausted')

    def _query(self, operation: str, payload: dict):
        try:
            return self._client().query(operation, payload)
        except StorageError as error:
            raise GoalRunRepositoryError(
                f'{operation} failed: {error.code}') from error

    def start(
        self,
        run_id: str,
        *,
        conversation_id: str,
        objective: str,
        definition: Mapping[str, Any],
        policy: Mapping[str, Any],
    ) -> dict:
        return self._command('goal.run.start', {
            **self._boundary(),
            'run_id': str(run_id),
            'conversation_id': str(conversation_id),
            'objective': str(objective),
            'definition': dict(definition),
            'policy': dict(policy),
        }, retry_conflict=True)

    def transition(
        self,
        run_id: str,
        *,
        status: str,
        reason: str,
        final: str = '',
        outcome: Mapping[str, Any] | None = None,
    ) -> dict:
        return self._command('goal.run.transition', {
            **self._boundary(),
            'run_id': str(run_id),
            'status': str(status),
            'reason': str(reason),
            'final': str(final or ''),
            'outcome': dict(outcome or {}),
        })

    def latest_for_conversation(self, conversation_id: str) -> dict | None:
        result = self._query('goal.run.latest', {
            **self._boundary(),
            'conversation_id': str(conversation_id),
        })
        return result if isinstance(result, dict) else None


__all__ = [
    'GoalRunRepositoryError',
    'GoalRunRepositoryPort',
    'SidecarGoalRunRepository',
]
