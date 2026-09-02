"""Sidecar repository for owner-scoped Git integration-control state.

This module is the only application boundary that knows the named storage
operations. User-facing reads and writes require an explicit principal;
queue-worker operations are deliberately system-scoped and return the owner
with every claimed row.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

from lib.storage import get_storage_client
from lib.storage.errors import StorageError


class IntegrationStateError(RuntimeError):
    """A rejected durable-state transition."""


def _command_id(operation: str, payload: dict[str, Any]) -> str:
    encoded = json.dumps(
        {'operation': operation, 'payload': payload}, ensure_ascii=False,
        sort_keys=True, separators=(',', ':')).encode('utf-8')
    return 'integration_' + hashlib.sha256(encoded).hexdigest()


def _command(operation: str, payload: dict[str, Any]) -> Any:
    try:
        return get_storage_client(write=True).command(
            operation, payload,
            _command_id(operation, payload), deadline=5.0)
    except StorageError as exc:
        if exc.code == 'database_conflict':
            raise IntegrationStateError(exc.message) from exc
        raise


def _query(operation: str, payload: dict[str, Any]) -> Any:
    try:
        return get_storage_client().query(operation, payload, deadline=5.0)
    except StorageError as exc:
        if exc.code == 'database_conflict':
            raise IntegrationStateError(exc.message) from exc
        raise


def initialize_store() -> None:
    """Assert that the already-supervised Sidecar schema is reachable."""
    _query('system.schema_version', {})


def register_workspace(
    *,
    user_id: int,
    project_root: str,
    task_id: str,
    title: str,
    workspace_path: str,
    managed: bool,
    base_sha: str,
    now: float,
    origin: dict[str, Any] | None = None,
) -> None:
    _command('integration.workspace.register', {
        'user_id': int(user_id),
        'project_root': project_root,
        'task_id': task_id,
        'title': title,
        'workspace_path': workspace_path,
        'managed': bool(managed),
        'base_sha': base_sha,
        'now': float(now),
        'origin_json': json.dumps(
            origin, ensure_ascii=False, sort_keys=True) if origin else '',
    })


def find_workspace(
    project_root: str, task_id: str, *, user_id: int,
) -> dict[str, Any] | None:
    result = _query('integration.workspace.get', {
        'user_id': int(user_id), 'project_root': project_root,
        'task_id': task_id,
    })
    return dict(result) if result is not None else None


def get_workspace(
    project_root: str, task_id: str, *, user_id: int,
) -> dict[str, Any]:
    result = find_workspace(project_root, task_id, user_id=user_id)
    if result is None:
        raise IntegrationStateError(f'Unknown integration task: {task_id}')
    return result


def save_checkpoint(
    *,
    user_id: int,
    project_root: str,
    task_id: str,
    checkpoint_sha: str,
    base_sha: str = '',
    now: float,
) -> None:
    _command('integration.workspace.save_checkpoint', {
        'user_id': int(user_id),
        'project_root': project_root,
        'task_id': task_id,
        'checkpoint_sha': checkpoint_sha,
        'base_sha': base_sha,
        'now': float(now),
    })


def submit_checkpoint(
    *,
    user_id: int,
    project_root: str,
    task_id: str,
    now: float,
) -> None:
    _command('integration.workspace.submit', {
        'user_id': int(user_id),
        'project_root': project_root,
        'task_id': task_id,
        'now': float(now),
    })


def retry_checkpoint(
    *,
    user_id: int,
    project_root: str,
    task_id: str,
    now: float,
) -> None:
    _command('integration.workspace.retry', {
        'user_id': int(user_id),
        'project_root': project_root,
        'task_id': task_id,
        'now': float(now),
    })

def discard_workspace(
    *,
    user_id: int,
    project_root: str,
    task_id: str,
    now: float,
) -> None:
    _command('integration.workspace.discard', {
        'user_id': int(user_id),
        'project_root': project_root,
        'task_id': task_id,
        'now': float(now),
    })


def set_workspace_meta(
    *,
    user_id: int,
    project_root: str,
    task_id: str,
    patch: dict[str, Any],
    now: float,
) -> dict[str, Any]:
    """Shallow-merge ``patch`` into the workspace's origin document."""
    result = _command('integration.workspace.set_meta', {
        'user_id': int(user_id),
        'project_root': project_root,
        'task_id': task_id,
        'patch_json': json.dumps(patch, ensure_ascii=False, sort_keys=True),
        'now': float(now),
    })
    return dict(result.get('origin') or {})


def claim_next(*, now: float) -> dict[str, Any] | None:
    result = _command('integration.workspace.claim_next', {'now': float(now)})
    return dict(result) if result is not None else None


def peek_ready(*, now: float | None = None) -> dict[str, Any] | None:
    """Read-only ready/stale-claim probe; never touches the writer lane."""
    payload = {} if now is None else {'now': float(now)}
    result = _query('integration.workspace.peek_ready', payload)
    return dict(result) if result is not None else None


def get_integrating(
    row_id: int,
) -> dict[str, Any] | None:
    result = _query(
        'integration.workspace.get_integrating', {'row_id': int(row_id)})
    return dict(result) if result is not None else None


def quarantine(
    *,
    row_id: int,
    reason: str,
    now: float,
) -> bool:
    result = _command('integration.workspace.quarantine', {
        'row_id': int(row_id),
        'error': reason,
        'candidate_sha': '',
        'now': float(now),
    })
    return bool(result['changed'])


def requeue(
    *,
    row_id: int,
    error: str,
    now: float,
) -> bool:
    result = _command('integration.workspace.requeue', {
        'row_id': int(row_id),
        'error': error,
        'candidate_sha': '',
        'now': float(now),
    })
    return bool(result['changed'])


def mark_merged(
    *,
    row_id: int,
    candidate_sha: str,
    now: float,
) -> bool:
    result = _command('integration.workspace.mark_merged', {
        'row_id': int(row_id),
        'candidate_sha': candidate_sha,
        'error': '',
        'now': float(now),
    })
    return bool(result['changed'])


def mark_failed(
    *,
    row_id: int,
    error: str,
    now: float,
) -> bool:
    result = _command('integration.workspace.mark_failed', {
        'row_id': int(row_id),
        'error': error,
        'candidate_sha': '',
        'now': float(now),
    })
    return bool(result['changed'])


def record_event(
    *,
    user_id: int,
    project_root: str,
    task_id: str,
    kind: str,
    message: str,
    detail: str,
    now: float,
) -> None:
    _command('integration.event.record', {
        'user_id': int(user_id),
        'project_root': project_root,
        'task_id': task_id,
        'kind': kind,
        'message': message,
        'detail': detail,
        'now': float(now),
    })


def status_rows(
    project_root: str, *, user_id: int,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    result = _query('integration.status', {
        'user_id': int(user_id), 'project_root': project_root})
    return list(result['rows']), list(result['events'])


__all__ = [
    'IntegrationStateError', 'claim_next', 'discard_workspace',
    'get_integrating', 'get_workspace',
    'initialize_store', 'mark_failed', 'mark_merged', 'peek_ready', 'quarantine',
    'record_event', 'register_workspace', 'requeue',
    'retry_checkpoint', 'save_checkpoint', 'set_workspace_meta', 'status_rows',
    'submit_checkpoint',
]
