"""Process-start recovery for durable orchestration run headers.

Entry point: :func:`retire_interrupted_orchestration_runs`.
Dependency: the Sidecar's explicit cross-owner maintenance operation. Normal
request-time run access remains scoped through ``SidecarOrchestrationRunStore``.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping

from lib.storage.client import StorageClient
from lib.storage.errors import StorageError


def retire_interrupted_orchestration_runs(
    *,
    error: dict | str,
    client: Callable[..., StorageClient] | None = None,
) -> int:
    """Settle every non-terminal run left behind by the previous process.

    Startup recovery is process authority, not user authority: executor
    threads for every owner died together. The dedicated maintenance
    operation makes that cross-owner scope explicit without hardcoding a
    personal user or weakening the owner filter on request-time operations.
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
    return max(0, int(result['retired']))


__all__ = ['retire_interrupted_orchestration_runs']
