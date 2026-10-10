"""Process-start recovery for durable orchestration run headers.

Entry points: :func:`retire_interrupted_orchestration_runs` and its detailed
variant :func:`retire_interrupted_orchestration_runs_detailed`, which also
returns fresh and durably pending run descriptors for the continuity
pass (docs/RUN_CONTINUITY.md D2).
Dependency: the Sidecar's explicit cross-owner maintenance operation. Normal
request-time run access remains scoped through ``SidecarOrchestrationRunStore``.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass

from lib.storage.client import StorageClient
from lib.storage.errors import StorageError


@dataclass(frozen=True)
class BootRetirement:
    """Fresh retirement count plus durable candidates elected for this boot."""

    retired: int
    candidates: tuple


def _validate_candidates(value) -> tuple:
    if value is None:
        return ()
    if not isinstance(value, list):
        raise StorageError(
            'database_protocol_error',
            'Malformed orchestration startup recovery candidates',
        )
    candidates = []
    for row in value:
        if not isinstance(row, Mapping) or not isinstance(
                row.get('run_id'), str) or not isinstance(
                row.get('user_id'), int):
            raise StorageError(
                'database_protocol_error',
                'Malformed orchestration continuity candidate',
            )
        candidates.append({
            'run_id': row['run_id'],
            'user_id': int(row['user_id']),
            'tenant_id': str(row.get('tenant_id') or ''),
            'created_by': str(row.get('created_by') or ''),
        })
    return tuple(candidates)


def retire_interrupted_orchestration_runs_detailed(
    *,
    error: dict | str,
    client: Callable[..., StorageClient] | None = None,
) -> BootRetirement:
    """Settle lost workers and re-elect unacknowledged Chat Goal handoffs.

    Startup recovery is process authority, not user authority: executor
    threads for every owner died together. The dedicated maintenance
    operation makes that cross-owner scope explicit without hardcoding a
    personal user or weakening the owner filter on request-time operations.
    The candidate descriptors carry each row's recorded owner; the boot
    continuity pass binds successors to exactly that identity.
    """
    if client is None:
        from lib.storage import get_storage_client
        client = get_storage_client
    result = client(write=True).maintenance(
        'orchestration.run.retire_interrupted_all',
        {'error': error},
    )
    if not isinstance(result, Mapping) or not isinstance(
        result.get('retired'), int
    ):
        raise StorageError(
            'database_protocol_error',
            'Malformed orchestration startup recovery result',
        )
    return BootRetirement(
        retired=max(0, int(result['retired'])),
        candidates=_validate_candidates(result.get('candidates')),
    )


def retire_interrupted_orchestration_runs(
    *,
    error: dict | str,
    client: Callable[..., StorageClient] | None = None,
) -> int:
    """Settle lost workers while preserving durable human continuations."""
    return retire_interrupted_orchestration_runs_detailed(
        error=error, client=client).retired


__all__ = [
    'BootRetirement',
    'retire_interrupted_orchestration_runs',
    'retire_interrupted_orchestration_runs_detailed',
]
