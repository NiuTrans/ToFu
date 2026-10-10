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
    """Finish orphaned work only after its workspace intent is acknowledged."""
    from lib.tasks_pkg.workspace_delivery import (
        settle_task_workspaces, task_outcome, work_result_summary,
    )

    client = get_storage_client(write=True)
    recovered = 0
    cursor = None
    while True:
        snapshot = client.maintenance(
            'project_brain.recovery.snapshot',
            {'cursor': cursor} if cursor is not None else {}, deadline=30.0)
        next_cursor = snapshot.get('nextCursor')
        if snapshot.get('capped') and (
                not isinstance(next_cursor, dict) or not next_cursor
                or next_cursor == cursor):
            raise RuntimeError(
                'Project Brain capped recovery page has no advancing cursor')
        # Legacy index rows may repeat a project within this bounded page.
        # Acknowledge each owner/project/work identity once; failed intents
        # remain eligible, and the next page still reads fresh authority state.
        settled_work = set()
        for project in snapshot.get('projects') or ():
            owner_user_id = int(project.get('ownerUserId') or 0)
            project_key = str(project.get('projectKey') or '')
            if not owner_user_id or not project_key:
                raise RuntimeError('Malformed Project Brain recovery scope')
            for work in project.get('workItems') or ():
                # Older native snapshots included terminal history. It is not
                # orphaned work: do not replay tasks or issue no-op writes for
                # it on every boot, even while a pinned binary is upgraded.
                if work.get('status') != 'active':
                    continue
                task_id = str(work.get('taskId') or '')
                work_id = str(work.get('id') or '')
                identity = (owner_user_id, project_key, work_id)
                if identity in settled_work:
                    continue
                task = client.query('task_results.replay_get', {
                    'key': task_id, 'user_id': owner_user_id,
                    'include_terminal_payload': False,
                    'include_metadata': True,
                })
                # replay_get stores terminal evidence in metadata, unlike the
                # in-memory task carrier. Copy only verdict fields; never infer
                # a finish reason from a nominal terminal status.
                task = dict(task or {})
                metadata = task.get('metadata')
                if isinstance(metadata, dict):
                    for key in ('finishReason', 'streamState', 'aborted', '_abort_reason'):
                        if key not in task and key in metadata:
                            task[key] = metadata[key]
                status = task_outcome(task)
                if status == 'cancelled':
                    summary = 'Execution was cancelled before server restart recovery.'
                elif status == 'completed':
                    summary = 'Execution completed before server restart recovery.'
                else:
                    error = (task or {}).get('error')
                    summary = str(error or 'Execution lacked verified completion before server restart.')
                # Recover only this work item's existing registration. Missing
                # execution evidence never licenses adoption of sibling trees.
                recovery_task = dict(task or {}, id=task_id, _userId=owner_user_id)
                recovery_task['config'] = dict(
                    (task or {}).get('config') or {}, projectPath=project_key)
                try:
                    receipt = settle_task_workspaces(recovery_task, project_key)
                    if receipt and (receipt.get('errors') or receipt.get('status') == 'error'):
                        raise RuntimeError('Workspace terminal intent remains unacknowledged')
                except Exception as exc:
                    # The active work item is the durable retry eligibility.
                    # Finishing it here would remove it from the next snapshot.
                    logger.warning(
                        '[ProjectBrain] recovery intent pending work=%s task=%s: %s',
                        work_id, task_id, exc)
                    continue
                delivery_summary = work_result_summary(recovery_task)
                if delivery_summary:
                    summary = summary[:3000] + '\n' + delivery_summary
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
                settled_work.add(identity)
                recovered += 1
        if not snapshot.get('capped'):
            break
        cursor = next_cursor
    if recovered:
        logger.warning(
            '[ProjectBrain] terminally reconciled %d orphaned work item(s)',
            recovered)
    return recovered


__all__ = ['ensure_project_brain_cutover', 'recover_active_work_items']
