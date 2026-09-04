"""Fail-closed Project Brain cutover and active-work restart reconciliation."""

from __future__ import annotations

import os

from lib.log import get_logger
from lib.storage import get_storage_client
from runtime_guards import storage_backup_timeout_seconds


logger = get_logger(__name__)


def ensure_project_brain_cutover() -> dict:
    """Back up the authority, migrate once, and verify the cutover receipt."""
    client = get_storage_client(write=True)
    status = client.query('project_brain.cutover.status', {})
    if status.get('complete'):
        return {'complete': True, 'alreadyComplete': True}
    health = client.health(deadline=5.0)
    backend = str(health.get('backend') or '')
    if backend == 'sqlite':
        backup = client.maintenance(
            'system.backup',
            deadline=float(storage_backup_timeout_seconds()),
        )
        if not isinstance(backup, dict) or not backup.get('ok', True):
            raise RuntimeError('Project Brain pre-cutover backup failed')
    elif backend == 'postgres':
        # External PostgreSQL backup is deliberately not faked inside the app.
        # Operators provide the receipt only after the platform snapshot exists.
        if os.environ.get(
                'TOFU_PROJECT_BRAIN_CUTOVER_BACKUP_CONFIRMED', '').strip() != '1':
            raise RuntimeError(
                'Set TOFU_PROJECT_BRAIN_CUTOVER_BACKUP_CONFIRMED=1 only after '
                'the platform PostgreSQL backup has completed')
    else:
        raise RuntimeError(
            f'Unsupported Project Brain cutover backup backend: {backend}')
    result = client.command(
        'project_brain.cutover', {}, 'project-brain-cutover-v1',
        deadline=120.0)
    verified = client.query('project_brain.cutover.status', {})
    if not result.get('verified') or not verified.get('complete'):
        raise RuntimeError('Project Brain cutover verification failed')
    return {'complete': True, **result}


def recover_active_work_items() -> int:
    """Finish orphaned active work from task authority; never reassign it."""
    client = get_storage_client(write=True)
    snapshot = client.maintenance(
        'project_brain.recovery.snapshot', {}, deadline=30.0)
    if snapshot.get('capped'):
        raise RuntimeError('Project Brain active recovery snapshot was capped')
    recovered = 0
    for project in snapshot.get('projects') or ():
        owner_user_id = int(project.get('ownerUserId') or 0)
        project_key = str(project.get('projectKey') or '')
        if not owner_user_id or not project_key:
            raise RuntimeError('Malformed Project Brain recovery scope')
        for work in project.get('workItems') or ():
            task_id = str(work.get('taskId') or '')
            work_id = str(work.get('id') or '')
            task = client.query('task_results.replay_get', {
                'key': task_id, 'user_id': owner_user_id,
                'include_terminal_payload': False,
            })
            raw_status = str((task or {}).get('status') or '')
            if raw_status in {'aborted', 'cancelled'}:
                status = 'cancelled'
                summary = 'Execution was cancelled before server restart recovery.'
            elif raw_status in {'done', 'completed', 'success'}:
                status = 'completed'
                summary = 'Execution completed before server restart recovery.'
            else:
                status = 'failed'
                error = (task or {}).get('error')
                summary = str(error or 'Execution was interrupted by server restart.')
            client.command(
                'project_brain.work.finish', {
                    'owner_user_id': owner_user_id,
                    'project_key': project_key,
                    'work_id': work_id,
                    'status': status,
                    'result_summary': summary[:4000],
                },
                f'project-work-recovery:{work_id}',
            )
            recovered += 1
    if recovered:
        logger.warning(
            '[ProjectBrain] terminally reconciled %d orphaned work item(s)',
            recovered)
    return recovered


__all__ = ['ensure_project_brain_cutover', 'recover_active_work_items']
