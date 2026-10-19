"""Project publication adapter over the existing workspace delivery owner.

Entry points: workspace_origin pins private admission before registration;
publication_reason checks live repository authority and immutable delta scope;
delivery_view projects origin.delivery without inventing execution state.
Repository validation owns source/grant/result and exact attempt/task identity.
No scheduler, durable ledger, tool authority or execution is created here.
"""
from __future__ import annotations

from collections.abc import Mapping
from copy import deepcopy
from pathlib import Path

from . import admission

from lib.log import get_logger

logger = get_logger(__name__)


def workspace_origin(task) -> dict:
    """Copy the admitted carrier into the FIRST registration, never backfill it."""
    config = (task or {}).get('config') or {}
    return {admission.KEY: deepcopy(config[admission.KEY])} if admission.KEY in config else {}


def _same_project(left, right) -> bool:
    return bool(left and right) and Path(left).resolve() == Path(right).resolve()


def _path_allowed(path: str, scopes, root: Path) -> bool:
    """The descriptor-I/O policy also fences external checkpoint mutations."""
    from .scope import relative_path_allowed
    return relative_path_allowed(path, scopes)


def publication_reason(row: Mapping, receipt: Mapping) -> str:
    """Return missing authority as a reason for existing `blocked`, not a state.

    Absent carriers preserve generic task delivery. Present malformed carriers,
    repository/read failures and unverifiable checkpoint scope fail closed.
    Called again inside canonical repository admission after unlocked gates.
    """
    origin = row.get('origin') or {}
    if admission.KEY not in origin:
        return ''
    binding = origin[admission.KEY]
    try:
        if (not isinstance(binding, Mapping)
                or type(row.get('user_id')) is not int or row['user_id'] < 1
                or row['user_id'] != binding.get('owner')
                or not row.get('task_id')
                or not _same_project(row.get('project_root'), binding.get('projectPath'))):
            return 'project_publication_identity_changed'
        assignment = admission.repository_for(binding).validate_publication(binding, row['task_id'])

        if (assignment.get('publication') or {}).get('checkpointSha') != row.get('checkpoint_sha'):
            return 'project_publication_checkpoint_changed'
        if (not isinstance(assignment, Mapping)
                or assignment.get('id') != binding.get('assignmentId')
                or assignment['grant']['owner'] != binding['owner']
                or not _same_project(assignment['request']['projectPath'], row['project_root'])
                or assignment['request']['recipientId'] != binding.get('recipientId')
                or assignment['grant']['revision'] != binding.get('revision')
                or assignment['grant']['revoked']):
            return 'project_publication_identity_changed'
    except Exception as diagnostic_error:
        logger.debug('publication_reason failure handled: %s', type(diagnostic_error).__name__)
        return 'project_publication_authority_missing'

    profile = assignment['request'].get('profile') or {}
    if profile.get('mode') not in ('files', 'project') or not isinstance(profile.get('writePaths'), list):
        return 'project_publication_scope_missing'
    # Source HEAD omits borrowed WIP. Only sourceSnapshot isolates task changes.
    baseline, checkpoint = origin.get('sourceSnapshot'), row.get('checkpoint_sha')
    if not baseline or not checkpoint:
        return 'project_publication_checkpoint_unavailable'
    pairs = [(baseline, checkpoint)]
    repair = receipt.get('repair') or {}
    if repair:
        if (repair.get('originalBaselineSha') != baseline
                or repair.get('registeredCheckpointSha') != checkpoint
                or not repair.get('baselineSha') or not repair.get('checkpointSha')):
            return 'project_publication_checkpoint_unavailable'
        pairs.append((repair['baselineSha'], repair['checkpointSha']))
    try:
        from lib import integration_control as ic
        root = Path(row['project_root']).resolve()
        for before, after in pairs:
            # Disable rename detection so BOTH old/deleted and new paths fence.
            paths = ic._git(root, ['diff', '--no-renames', '--name-only', '-z',
                                   before, after], check=True).stdout.split('\0')
            if any(path and not _path_allowed(path, profile['writePaths'], root) for path in paths):
                return 'project_publication_write_scope_exceeded'
    except Exception as diagnostic_error:
        logger.debug('publication_reason failure handled: %s', type(diagnostic_error).__name__)
        return 'project_publication_checkpoint_unavailable'
    return ''


def delivery_view(assignment: Mapping, attempt: Mapping | None, workspace_get) -> dict | None:
    """Read the exact associated workspace receipt, never infer from candidate.

    workspace_get uses the existing (project_root, task_id, user_id=owner) port.
    Absence or unavailable storage is unknown delivery, not canonical success.
    This is diagnostic projection; publication_reason remains the live fence.
    """
    execution = assignment.get('execution') or {}
    if not isinstance(attempt, Mapping) or not attempt.get('taskId'):
        return None
    request, grant = assignment['request'], assignment['grant']
    if (attempt.get('attemptId') != execution.get('attemptId')
            or attempt.get('turnId') != execution.get('turnId')
            or attempt.get('conversationId') != request['recipientId']):
        return None
    task_id = attempt['taskId']
    try:
        row = workspace_get(request['projectPath'], task_id, user_id=grant['owner'])
        if (not isinstance(row, Mapping) or row.get('task_id') != task_id
                or type(row.get('user_id')) is not int or row['user_id'] < 1
                or row['user_id'] != grant['owner']
                or not _same_project(row.get('project_root'), request['projectPath'])):
            return None
        origin = row.get('origin') or {}
        binding = origin.get(admission.KEY) or {}
        if (binding.get('assignmentId') != assignment['id']
                or binding.get('owner') != grant['owner']
                or binding.get('recipientId') != request['recipientId']
                or not _same_project(binding.get('projectPath'), request['projectPath'])):
            return None
        receipt = origin.get('delivery') or {}
        if not receipt:
            return None
        return {'taskId': task_id, 'status': str(receipt.get('state') or 'unknown')[:64],
                'reason': str(receipt.get('reason') or '')[:1024],
                'checkpointSha': str(receipt.get('checkpointSha') or row.get('checkpoint_sha') or '')[:256],
                'targetSha': str(receipt.get('targetSha') or '')[:256]}
    except Exception as diagnostic_error:
        logger.debug('delivery_view failure handled: %s', type(diagnostic_error).__name__)
        return None
