"""Deterministic Git integration-control storage operations."""

from __future__ import annotations

from collections.abc import Mapping
import json
import math
from typing import Any

from lib.storage.errors import StorageError
from lib.storage_sidecar import operations as ops
from lib.storage_sidecar.adapters.base import Session
from lib.storage_sidecar.faults import inject_once


def _text(
    payload: Mapping[str, Any], key: str, maximum: int = 4096,
    *, required: bool = True,
) -> str:
    value = payload.get(key, '')
    if not isinstance(value, str) or len(value) > maximum:
        raise StorageError(
            'database_protocol_error',
            f'Invalid {key} in integration storage request')
    if required and not value:
        raise StorageError(
            'database_protocol_error',
            f'Missing {key} in integration storage request')
    return value


def _integer(payload: Mapping[str, Any], key: str, *, minimum: int = 0) -> int:
    value = payload.get(key)
    if (not isinstance(value, int) or isinstance(value, bool)
            or value < minimum or value > 9_223_372_036_854_775_807):
        raise StorageError(
            'database_protocol_error',
            f'Invalid {key} in integration storage request')
    return value


def _number(payload: Mapping[str, Any], key: str) -> float:
    value = payload.get(key)
    if (not isinstance(value, (int, float)) or isinstance(value, bool)
            or not math.isfinite(float(value)) or float(value) < 0):
        raise StorageError(
            'database_protocol_error',
            f'Invalid {key} in integration storage request')
    return float(value)


def _origin_document(raw: Any) -> dict[str, Any]:
    """Parse the meta row's origin JSON — best-effort, never raises."""
    if not raw or not isinstance(raw, str):
        return {}
    try:
        value = json.loads(raw)
    except (ValueError, TypeError):
        return {}
    return value if isinstance(value, dict) else {}


def _row_document(row: Mapping[str, Any]) -> dict[str, Any]:
    return {
        'id': int(row['id']),
        'user_id': int(row['user_id']),
        'project_root': str(row['project_root']),
        'task_id': str(row['task_id']),
        'title': str(row['title'] or ''),
        'workspace_path': str(row['workspace_path']),
        'managed': int(row['managed']),
        'state': str(row['state']),
        'base_sha': str(row['base_sha'] or ''),
        'checkpoint_sha': str(row['checkpoint_sha'] or ''),
        'candidate_sha': str(row['candidate_sha'] or ''),
        'error': str(row['error'] or ''),
        'origin': _origin_document(row.get('origin_json')),
        'created_at': float(row['created_at']),
        'updated_at': float(row['updated_at']),
    }


def _event_document(row: Mapping[str, Any]) -> dict[str, Any]:
    return {
        'id': int(row['id']),
        'user_id': int(row['user_id']),
        'project_root': str(row['project_root']),
        'task_id': str(row['task_id'] or ''),
        'kind': str(row['kind']),
        'message': str(row['message'] or ''),
        'detail': str(row['detail'] or ''),
        'created_at': float(row['created_at']),
    }


def _workspace_row(
    session: Session, user_id: int, project_root: str, task_id: str,
) -> Mapping[str, Any] | None:
    return session.fetch_one(
        'SELECT w.id, w.user_id, w.project_root, w.task_id, w.title, '
        'w.workspace_path, '
        'w.managed, w.state, w.base_sha, w.checkpoint_sha, w.candidate_sha, '
        'w.error, w.created_at, w.updated_at, m.origin_json '
        'FROM integration_workspaces w '
        'LEFT JOIN integration_workspace_meta m '
        'ON m.user_id = w.user_id AND m.project_root = w.project_root '
        'AND m.task_id = w.task_id '
        'WHERE w.user_id = ? AND w.project_root = ? AND w.task_id = ?',
        (user_id, project_root, task_id))


def _required_workspace(
    session: Session, user_id: int, project_root: str, task_id: str,
) -> Mapping[str, Any]:
    row = _workspace_row(session, user_id, project_root, task_id)
    if row is None:
        raise StorageError(
            'database_conflict', 'Unknown integration task')
    return row


def _next_id(session: Session, table: str) -> int:
    # table is selected only by this module, never supplied by the caller.
    row = session.fetch_one(f'SELECT COALESCE(MAX(id), 0) AS value FROM {table}')
    return int(row['value'] if row else 0) + 1


def _append_event(
    session: Session, user_id: int, project_root: str, task_id: str, kind: str,
    message: str, detail: str, now: float,
) -> None:
    session.lock_key('integration.events', f'{user_id}|{project_root}')
    event_id = _next_id(session, 'integration_events')
    session.execute(
        'INSERT INTO integration_events('
        'id, user_id, project_root, task_id, kind, message, detail, created_at) '
        'VALUES (?, ?, ?, ?, ?, ?, ?, ?)',
        (event_id, user_id, project_root, task_id, kind, message[:500],
         detail[:4000], now))
    session.execute(
        'DELETE FROM integration_events WHERE user_id = ? AND project_root = ? '
        'AND id NOT IN (SELECT id FROM integration_events '
        'WHERE user_id = ? AND project_root = ? '
        'ORDER BY id DESC LIMIT 300)',
        (user_id, project_root, user_id, project_root))


def _register_workspace(session: Session, payload: Mapping[str, Any]) -> Any:
    user_id = _integer(payload, 'user_id', minimum=1)
    project_root = _text(payload, 'project_root')
    task_id = _text(payload, 'task_id', 512)
    title = _text(payload, 'title', 1000, required=False)
    workspace_path = _text(payload, 'workspace_path')
    base_sha = _text(payload, 'base_sha', 200)
    origin_json = _text(payload, 'origin_json', 4000, required=False)
    managed = payload.get('managed')
    if not isinstance(managed, bool):
        raise StorageError(
            'database_protocol_error',
            'Invalid managed in integration storage request')
    now = _number(payload, 'now')
    session.lock_key(
        'integration.workspace', f'{user_id}|{project_root}|{task_id}')
    existing = _workspace_row(session, user_id, project_root, task_id)
    if existing is not None and existing['state'] in {
            'ready', 'integrating', 'merged', 'discarded'}:
        raise StorageError(
            'database_conflict',
            'This task already has an immutable or terminal integration record')
    if existing is None:
        session.lock_key('integration.workspace_ids', 'global')
        session.execute(
            'INSERT INTO integration_workspaces('
            'id, user_id, project_root, task_id, title, workspace_path, '
            'managed, state, base_sha, created_at, updated_at) '
            'VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)',
            (_next_id(session, 'integration_workspaces'), user_id, project_root,
             task_id, title, workspace_path, int(managed), 'running', base_sha,
             now, now))
    else:
        session.execute(
            'UPDATE integration_workspaces SET title = ?, workspace_path = ?, '
            'managed = ?, base_sha = ?, checkpoint_sha = ?, candidate_sha = ?, '
            'state = ?, error = ?, updated_at = ? WHERE id = ?',
            (title, workspace_path, int(managed), base_sha, '', '', 'running',
             '', now, int(existing['id'])))
    if origin_json:
        _upsert_meta(
            session, user_id, project_root, task_id, origin_json, now)
    inject_once('integration.after_workspace_mutation')
    _append_event(
        session, user_id, project_root, task_id, 'registered',
        'Writer workspace registered', workspace_path, now)
    return {'ok': True}


def _upsert_meta(
    session: Session, user_id: int, project_root: str, task_id: str,
    origin_json: str, now: float,
) -> None:
    """Write the meta row's origin document (insert or replace)."""
    session.lock_key(
        'integration.workspace_meta', f'{user_id}|{project_root}|{task_id}')
    changed = session.execute(
        'UPDATE integration_workspace_meta SET origin_json = ?, updated_at = ? '
        'WHERE user_id = ? AND project_root = ? AND task_id = ?',
        (origin_json, now, user_id, project_root, task_id))
    if changed == 0:
        session.execute(
            'INSERT INTO integration_workspace_meta('
            'user_id, project_root, task_id, origin_json, updated_at) '
            'VALUES (?, ?, ?, ?, ?)',
            (user_id, project_root, task_id, origin_json, now))


def _get_workspace(session: Session, payload: Mapping[str, Any]) -> Any:
    row = _workspace_row(
        session, _integer(payload, 'user_id', minimum=1),
        _text(payload, 'project_root'),
        _text(payload, 'task_id', 512))
    return _row_document(row) if row is not None else None


def _save_checkpoint(session: Session, payload: Mapping[str, Any]) -> Any:
    user_id = _integer(payload, 'user_id', minimum=1)
    project_root = _text(payload, 'project_root')
    task_id = _text(payload, 'task_id', 512)
    checkpoint_sha = _text(payload, 'checkpoint_sha', 200)
    base_sha = _text(payload, 'base_sha', 200, required=False)
    now = _number(payload, 'now')
    session.lock_key(
        'integration.workspace', f'{user_id}|{project_root}|{task_id}')
    row = _required_workspace(session, user_id, project_root, task_id)
    if row['state'] in {'ready', 'integrating'}:
        raise StorageError(
            'database_conflict',
            'The submitted checkpoint is immutable while it is in the '
            'integration queue')
    if row['state'] in {'discarded', 'merged'}:
        raise StorageError(
            'database_conflict',
            f"The workspace is {row['state']}; checkpointing it would "
            'resurrect a terminal integration record')
    if row['state'] not in {'running', 'checkpointed', 'quarantined', 'failed'}:
        raise StorageError(
            'database_conflict',
            f"Workspace state {row['state']} cannot be checkpointed")
    if base_sha:
        session.execute(
            'UPDATE integration_workspaces SET checkpoint_sha = ?, base_sha = ?, '
            'state = ?, error = ?, updated_at = ? WHERE id = ?',
            (checkpoint_sha, base_sha, 'checkpointed', '', now, int(row['id'])))
    else:
        session.execute(
            'UPDATE integration_workspaces SET checkpoint_sha = ?, state = ?, '
            'error = ?, updated_at = ? WHERE id = ?',
            (checkpoint_sha, 'checkpointed', '', now, int(row['id'])))
    _append_event(
        session, user_id, project_root, task_id, 'checkpointed',
        f'Checkpoint {checkpoint_sha[:12]} captured without staging the workspace',
        '', now)
    return {'ok': True}


def _submit_checkpoint(session: Session, payload: Mapping[str, Any]) -> Any:
    user_id = _integer(payload, 'user_id', minimum=1)
    project_root = _text(payload, 'project_root')
    task_id = _text(payload, 'task_id', 512)
    now = _number(payload, 'now')
    session.lock_key(
        'integration.workspace', f'{user_id}|{project_root}|{task_id}')
    row = _required_workspace(session, user_id, project_root, task_id)
    if row['state'] != 'checkpointed':
        raise StorageError(
            'database_conflict',
            'Only a freshly checkpointed workspace can be submitted')
    if not row['checkpoint_sha']:
        raise StorageError(
            'database_conflict', 'Checkpoint the workspace before submitting')
    if row['state'] == 'integrating':
        raise StorageError(
            'database_conflict', 'The checkpoint is already integrating')
    session.execute(
        'UPDATE integration_workspaces SET state = ?, error = ?, updated_at = ? '
        'WHERE id = ?', ('ready', '', now, int(row['id'])))
    _append_event(
        session, user_id, project_root, task_id, 'submitted',
        'Checkpoint entered the deterministic integration queue', '', now)
    return {'ok': True}


def _retry_checkpoint(session: Session, payload: Mapping[str, Any]) -> Any:
    user_id = _integer(payload, 'user_id', minimum=1)
    project_root = _text(payload, 'project_root')
    task_id = _text(payload, 'task_id', 512)
    now = _number(payload, 'now')
    session.lock_key(
        'integration.workspace', f'{user_id}|{project_root}|{task_id}')
    row = _required_workspace(session, user_id, project_root, task_id)
    if row['state'] not in {'quarantined', 'failed'}:
        raise StorageError(
            'database_conflict',
            'Only quarantined or failed checkpoints can be retried')
    if not row['checkpoint_sha']:
        raise StorageError(
            'database_conflict', 'Checkpoint the workspace before retrying')
    session.execute(
        'UPDATE integration_workspaces SET state = ?, error = ?, updated_at = ? '
        'WHERE id = ?', ('ready', '', now, int(row['id'])))
    _append_event(
        session, user_id, project_root, task_id, 'retried',
        'Quarantined checkpoint returned to the queue', '', now)
    return {'ok': True}


_WORKSPACE_COLUMNS = (
    'id, user_id, project_root, task_id, title, workspace_path, managed, '
    'state, base_sha, checkpoint_sha, candidate_sha, error, created_at, '
    'updated_at')
# Oldest ready row whose project has no active integration — the single
# eligibility predicate shared by the read-only peek and the claiming CAS.
_READY_SELECT = (
    'SELECT ' + _WORKSPACE_COLUMNS + ' FROM integration_workspaces '
    'WHERE state = ? '
    'AND NOT EXISTS (SELECT 1 FROM integration_workspaces active '
    'WHERE active.project_root = integration_workspaces.project_root '
    'AND active.state = ?) ORDER BY updated_at ASC, id ASC LIMIT 1')


def _peek_ready(session: Session, payload: Mapping[str, Any]) -> Any:
    """Read-only 'is there claim/recovery work?' probe for the worker.

    Same ready eligibility predicate as ``_claim_next`` but WITHOUT a write.
    When the caller supplies ``now``, also report an abandoned integrating
    row after the same 660-second horizon. That lets every idle poll stay on
    the read pool while preserving crash recovery without periodic fsyncs.
    """
    row = session.fetch_one(_READY_SELECT, ('ready', 'integrating'))
    if row is None and payload.get('now') is not None:
        now = _number(payload, 'now')
        row = session.fetch_one(
            'SELECT ' + _WORKSPACE_COLUMNS + ' FROM integration_workspaces '
            'WHERE state = ? AND updated_at < ? '
            'ORDER BY updated_at ASC, id ASC LIMIT 1',
            ('integrating', now - 660))
    return _row_document(row) if row is not None else None


def _claim_next(session: Session, payload: Mapping[str, Any]) -> Any:
    now = _number(payload, 'now')
    session.lock_key('integration.claim', 'global')
    session.execute(
        'UPDATE integration_workspaces SET state = ?, error = ?, updated_at = ? '
        'WHERE state = ? AND updated_at < ?',
        ('ready', 'Recovered an interrupted integration', now, 'integrating',
         now - 660))
    row = session.fetch_one(_READY_SELECT, ('ready', 'integrating'))
    if row is None:
        return None
    changed = session.execute(
        'UPDATE integration_workspaces SET state = ?, updated_at = ? '
        'WHERE id = ? AND state = ?',
        ('integrating', now, int(row['id']), 'ready'))
    if changed != 1:
        return None
    fresh = session.fetch_one(
        'SELECT id, user_id, project_root, task_id, title, workspace_path, managed, '
        'state, base_sha, checkpoint_sha, candidate_sha, error, created_at, '
        'updated_at FROM integration_workspaces WHERE id = ?',
        (int(row['id']),))
    return _row_document(fresh) if fresh is not None else None


def _get_integrating(session: Session, payload: Mapping[str, Any]) -> Any:
    row = session.fetch_one(
        'SELECT id, user_id, project_root, task_id, title, workspace_path, managed, '
        'state, base_sha, checkpoint_sha, candidate_sha, error, created_at, '
        'updated_at FROM integration_workspaces WHERE id = ? AND state = ?',
        (_integer(payload, 'row_id', minimum=1), 'integrating'))
    return _row_document(row) if row is not None else None


def _cas_state(
    session: Session, payload: Mapping[str, Any], *, state: str,
    event_kind: str | None = None, event_message: str = '',
) -> Any:
    row_id = _integer(payload, 'row_id', minimum=1)
    now = _number(payload, 'now')
    error = _text(payload, 'error', 4000, required=False)
    candidate_sha = _text(payload, 'candidate_sha', 200, required=False)
    row = session.fetch_one(
        'SELECT user_id, project_root, task_id FROM integration_workspaces '
        'WHERE id = ? AND state = ?', (row_id, 'integrating'))
    if row is None:
        return {'changed': False}
    assignments = 'state = ?, error = ?, updated_at = ?'
    params: list[Any] = [state, error[:4000], now]
    if state == 'merged':
        assignments += ', candidate_sha = ?'
        params.append(candidate_sha)
    params.extend((row_id, 'integrating'))
    changed = session.execute(
        f'UPDATE integration_workspaces SET {assignments} '
        'WHERE id = ? AND state = ?', tuple(params))
    if changed == 1 and event_kind:
        detail = error if state in {'quarantined', 'failed'} else ''
        _append_event(
            session, int(row['user_id']), str(row['project_root']),
            str(row['task_id']), event_kind, event_message,
            detail, now)
    return {'changed': changed == 1}


def _quarantine(session: Session, payload: Mapping[str, Any]) -> Any:
    return _cas_state(
        session, payload, state='quarantined', event_kind='quarantined',
        event_message='Checkpoint needs attention')


def _requeue(session: Session, payload: Mapping[str, Any]) -> Any:
    return _cas_state(session, payload, state='ready')


def _mark_merged(session: Session, payload: Mapping[str, Any]) -> Any:
    candidate_sha = _text(payload, 'candidate_sha', 200)
    enriched = dict(payload)
    enriched['candidate_sha'] = candidate_sha
    enriched['error'] = ''
    return _cas_state(
        session, enriched, state='merged', event_kind='merged',
        event_message=f'Checkpoint integrated into candidate {candidate_sha[:12]}')


def _mark_failed(session: Session, payload: Mapping[str, Any]) -> Any:
    return _cas_state(
        session, payload, state='failed', event_kind='failed',
        event_message='Integration worker failed')


def _discard(session: Session, payload: Mapping[str, Any]) -> Any:
    """Human discard of a workspace row — the poisoned-queue escape hatch.

    A quarantined checkpoint that will never merge cleanly used to have
    exactly one verb (retry, which re-runs the same deterministic merge and
    fails again). Discard parks the row terminally: the integration queue
    never picks it up again, while the Git refs and the worktree directory
    stay on disk for forensics. More work requires a new task id.
    """
    user_id = _integer(payload, 'user_id', minimum=1)
    project_root = _text(payload, 'project_root')
    task_id = _text(payload, 'task_id', 512)
    now = _number(payload, 'now')
    session.lock_key(
        'integration.workspace', f'{user_id}|{project_root}|{task_id}')
    row = _required_workspace(session, user_id, project_root, task_id)
    if row['state'] == 'integrating':
        raise StorageError(
            'database_conflict',
            'Cannot discard while the checkpoint is integrating')
    if row['state'] == 'merged':
        raise StorageError(
            'database_conflict',
            'A merged integration record is terminal and cannot be discarded')
    if row['state'] == 'discarded':
        return {'ok': True, 'changed': False}
    changed = session.execute(
        'UPDATE integration_workspaces SET state = ?, updated_at = ? '
        'WHERE id = ?', ('discarded', now, int(row['id'])))
    _append_event(
        session, user_id, project_root, task_id, 'discarded',
        'Workspace discarded; refs and worktree kept for forensics', '', now)
    return {'ok': True, 'changed': changed == 1}


def _set_meta(session: Session, payload: Mapping[str, Any]) -> Any:
    """Shallow-merge keys into a workspace's origin document."""
    user_id = _integer(payload, 'user_id', minimum=1)
    project_root = _text(payload, 'project_root')
    task_id = _text(payload, 'task_id', 512)
    patch_json = _text(payload, 'patch_json', 4000)
    now = _number(payload, 'now')
    try:
        patch = json.loads(patch_json)
    except ValueError:
        raise StorageError(
            'database_protocol_error',
            'Invalid patch_json in integration storage request') from None
    if not isinstance(patch, dict):
        raise StorageError(
            'database_protocol_error',
            'Invalid patch_json in integration storage request')
    _required_workspace(session, user_id, project_root, task_id)
    session.lock_key(
        'integration.workspace_meta', f'{user_id}|{project_root}|{task_id}')
    existing = session.fetch_one(
        'SELECT origin_json FROM integration_workspace_meta '
        'WHERE user_id = ? AND project_root = ? AND task_id = ?',
        (user_id, project_root, task_id))
    merged = _origin_document(
        existing['origin_json'] if existing is not None else '')
    merged.update(patch)
    _upsert_meta(session, user_id, project_root, task_id,
                 json.dumps(merged, ensure_ascii=False, sort_keys=True), now)
    return {'ok': True, 'origin': merged}

def _record_event(session: Session, payload: Mapping[str, Any]) -> Any:
    _append_event(
        session, _integer(payload, 'user_id', minimum=1),
        _text(payload, 'project_root'),
        _text(payload, 'task_id', 512, required=False),
        _text(payload, 'kind', 200),
        _text(payload, 'message', 500, required=False),
        _text(payload, 'detail', 4000, required=False),
        _number(payload, 'now'))
    return {'ok': True}


def _status(session: Session, payload: Mapping[str, Any]) -> Any:
    user_id = _integer(payload, 'user_id', minimum=1)
    project_root = _text(payload, 'project_root')
    rows = session.fetch_all(
        'SELECT w.id, w.user_id, w.project_root, w.task_id, w.title, '
        'w.workspace_path, '
        'w.managed, w.state, w.base_sha, w.checkpoint_sha, w.candidate_sha, '
        'w.error, w.created_at, w.updated_at, m.origin_json '
        'FROM integration_workspaces w '
        'LEFT JOIN integration_workspace_meta m '
        'ON m.user_id = w.user_id AND m.project_root = w.project_root '
        'AND m.task_id = w.task_id '
        'WHERE w.user_id = ? AND w.project_root = ? '
        'ORDER BY w.updated_at DESC', (user_id, project_root))
    events = session.fetch_all(
        'SELECT id, user_id, project_root, task_id, kind, message, detail, '
        'created_at FROM integration_events '
        'WHERE user_id = ? AND project_root = ? '
        'ORDER BY id DESC LIMIT 30', (user_id, project_root))
    return {
        'rows': [_row_document(row) for row in rows],
        'events': [_event_document(row) for row in events],
    }


OPERATIONS = {
    'integration.workspace.register': ops.OperationSpec(
        'command', True, _register_workspace),
    'integration.workspace.get': ops.OperationSpec(
        'query', False, _get_workspace),
    'integration.workspace.save_checkpoint': ops.OperationSpec(
        'command', True, _save_checkpoint),
    'integration.workspace.submit': ops.OperationSpec(
        'command', True, _submit_checkpoint),
    'integration.workspace.retry': ops.OperationSpec(
        'command', True, _retry_checkpoint),
    'integration.workspace.discard': ops.OperationSpec(
        'command', True, _discard),
    'integration.workspace.set_meta': ops.OperationSpec(
        'command', True, _set_meta),
    'integration.workspace.claim_next': ops.OperationSpec(
        'command', True, _claim_next),
    'integration.workspace.peek_ready': ops.OperationSpec(
        'query', False, _peek_ready),
    'integration.workspace.get_integrating': ops.OperationSpec(
        'query', False, _get_integrating),
    'integration.workspace.quarantine': ops.OperationSpec(
        'command', True, _quarantine),
    'integration.workspace.requeue': ops.OperationSpec(
        'command', True, _requeue),
    'integration.workspace.mark_merged': ops.OperationSpec(
        'command', True, _mark_merged),
    'integration.workspace.mark_failed': ops.OperationSpec(
        'command', True, _mark_failed),
    'integration.event.record': ops.OperationSpec(
        'command', True, _record_event),
    'integration.status': ops.OperationSpec('query', False, _status),
}


__all__ = ['OPERATIONS']
