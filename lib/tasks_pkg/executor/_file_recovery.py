"""Admission for a new write to a file with an unresolved producer occurrence.

Entry points: check_file_recovery, recovery_receipt.
The selected Turn/file authorities supply fresh facts; existing task-local reads
and exact human receipts carry observation/decision, never a replay journal.
No handler, query, approval or file lease runs under the task projection lock.
"""
from contextlib import nullcontext
from dataclasses import dataclass
import hashlib
import json
import os

from lib.tools.result_envelope import typed_tool_error

from lib.log import get_logger

logger = get_logger(__name__)


@dataclass(frozen=True)
class FileRecoveryCheck:
    refusal: str | None = None
    unknown: tuple = ()

    @property
    def fingerprint(self):
        return hashlib.sha256(json.dumps(self.unknown, sort_keys=True,
            separators=(',', ':'), ensure_ascii=False).encode()).hexdigest()


def _identity(task):
    from lib.tasks_pkg.manager import task_user_id
    return task.get('id'), task.get('convId'), task.get('_attemptId'), task_user_id(task)


def _lock(task):
    return task.get('content_lock') or nullcontext()


def _sources(task):
    with _lock(task):
        rows = tuple(task.get('_checkpointToolRounds') or ()) + tuple(task.get('toolRounds') or ())
        selected = []
        for row in rows:
            if not isinstance(row, dict) or row.get('toolName') != 'write_file':
                continue
            effect = row.get('executionEffect')
            if (not isinstance(effect, dict) or effect.get('scope') not in ('attempt', 'invocation')
                    or effect.get('recovery') != 'reconcile'
                    or not (effect.get('state') in ('entered', 'unknown')
                            or effect.get('state') == 'returned' and row.get('status') in ('error', 'aborted', 'rejected'))
                    or row.get('_providerAttemptDiscarded') is True):
                continue
            publication = effect.get('reconciliation')
            if not isinstance(publication, dict) or publication.get('kind') != 'project_file_write':
                continue
            reference = effect.get('invocationInspection' if effect['scope'] == 'invocation' else 'inspection')
            selected.append((row.get('taskId'), effect.get('operationId'),
                             dict(reference) if isinstance(reference, dict) else {}, dict(publication)))
        return selected


def _key(reference):
    child = 'invocationId' in reference
    return (reference.get('conversationId'), 'invocation' if child else 'turn',
            reference.get('invocationId' if child else 'turnId'), reference.get('operationId'))


def _fact(observation):
    return (*_key(observation), observation.get('outcome'), observation.get('reason'))


def _inspection_arguments(reference):
    child = 'invocationId' in reference
    return {'operation_id': reference.get('operationId'),
            ('invocation_id' if child else 'turn_id'): reference.get('invocationId' if child else 'turnId')}


def _observations(task, current_round):
    """Use only an earlier, complete model-visible result in this executor.

    Existing settled toolContent is the receipt, after output budgeting. No
    result cache or observation journal is introduced; restart reads again.
    """
    if type(current_round) is not int:
        return {}
    with _lock(task):
        rows = tuple(task.get('toolRounds') or ())
    latest = {}
    for row in rows:
        if not isinstance(row, dict) or row.get('toolName') != 'inspect_tool_effect':
            continue
        number = row.get('llmRound')
        effect = row.get('executionEffect')
        if not isinstance(effect, dict):
            continue
        if (type(number) is not int or number >= current_round or row.get('status') != 'done'
                or row.get('taskId', task.get('id')) != task.get('id')
                or row.get('attemptId', task.get('_attemptId')) != task.get('_attemptId')
                or effect.get('state') != 'returned' or effect.get('recovery') != 'read_only'
                or row.get('_providerAttemptDiscarded') is True):
            continue
        try:
            observation = json.loads(row.get('toolContent') or '')
            from lib.conversation_sync.validation import decode
            observation = decode('InvocationToolEffectInspection' if 'invocationId' in observation
                                 else 'ConversationToolEffectInspection', observation)
        except (ValueError, TypeError):
            continue
        key = _key(observation)
        if key not in latest or number >= latest[key][0]:
            latest[key] = number, _fact(observation)
    return {key: value[1] for key, value in latest.items()}


def _refusal(code, message, reference=None):
    action = 'Inspect the unresolved file operation before a new write; independent work may continue.'
    if reference:
        arguments = _inspection_arguments(reference)
        action = 'Call inspect_tool_effect(' + json.dumps(arguments, ensure_ascii=False,
            separators=(',', ':')) + '). Replan before another write.'
        from lib.tools.result_envelope import tool_error_next_action
        if tool_error_next_action(action) != action:
            message += ' Exact inspection: ' + action
            action = 'Inspect the exact producing operation described in the error, then replan the new write.'
    return FileRecoveryCheck(refusal=typed_tool_error(code, message=message,
        retryable=False, next_action=action).to_model_text())


def check_file_recovery(task, name, arguments, row, project_path):
    """Freshly validate affected occurrences before ordinary approval/entry.

    Only tracked local write_file is covered. Equal arguments never identify a
    prior operation. Target matching scopes dependencies, not replay identity.
    """
    if name != 'write_file':
        return FileRecoveryCheck()
    sources = _sources(task)
    if not sources:
        return FileRecoveryCheck()
    from lib.tasks_pkg.handlers._read_gate import _resolve_abs
    from lib.tasks_pkg.workspace_delivery import execution_root_key
    target = _resolve_abs(project_path, execution_root_key(task), arguments.get('path'))
    if not target:
        return _refusal('tool_effect_target_unavailable', 'The new file target could not be resolved; no write started.')
    target = os.path.realpath(target)
    from lib.conversation_sync.turn_images import turn_image_owner_scope
    scope = turn_image_owner_scope(_identity(task)[3], task.get('convId'))
    seen, unknown = set(), []
    observations = None
    for producer, operation, reference, publication in sources:
        base, path = publication.get('basePath'), publication.get('path')
        if not isinstance(base, str) or not isinstance(path, str):
            return _refusal('tool_effect_evidence_invalid', 'An unresolved file occurrence has invalid target evidence.')
        if os.path.realpath(os.path.join(base, path)) != target:
            continue
        key = _key(reference)
        child = 'invocationId' in reference
        locator = reference.get('invocationId' if child else 'turnId')
        if (reference.get('conversationId') != task.get('convId') or reference.get('ownerScope') != scope
                or reference.get('operationId') != operation or publication.get('taskId') != producer
                or not isinstance(operation, str) or len(operation) != 32
                or not isinstance(locator, str) or not locator
                or child and (locator != producer or 'turnId' in reference)):
            return _refusal('tool_effect_evidence_invalid', 'The affected file occurrence has no valid owned inspection identity.')
        if key in seen:
            continue  # The authoritative query still rejects duplicate stored occurrences.
        seen.add(key)
        if observations is None:
            observations = _observations(task, row.get('llmRound'))
        observed = observations.get(key)
        if observed is None:
            return _refusal('tool_effect_inspection_required',
                'A prior write to this file is unresolved. Read its publication evidence before planning this new write.', reference)
        from lib.tasks_pkg.handlers.tool_effect_inspection import inspect_for_task
        try:
            current = inspect_for_task(task, _inspection_arguments(reference))
        except Exception as diagnostic_error:
            logger.debug('check_file_recovery failure handled: %s', type(diagnostic_error).__name__)
            return _refusal('tool_effect_inspection_unavailable',
                'The owning file service could not verify the prior operation; no new write started.', reference)
        if _fact(current) != observed:
            return _refusal('tool_effect_inspection_changed',
                'File-publication evidence changed after inspection. Read it again before planning a new write.', reference)
        if current['reason'] in ('writer_active', 'evidence_unavailable'):
            return _refusal('tool_effect_inspection_unavailable',
                'The file writer is active or its evidence is unavailable. Wait for it to settle, then inspect again.', reference)
        if current['outcome'] == 'unknown':
            unknown.append(_fact(current))
    return FileRecoveryCheck(unknown=tuple(sorted(unknown)))


def recovery_receipt(task, name, call_id, arguments, check, *, consume=False):
    """Only an exact fresh human decision on these unknown facts permits entry."""
    from lib.tasks_pkg.tool_dispatch._approval import approval_receipt_authority
    if approval_receipt_authority(task, name, call_id, arguments) != 'human':
        return False
    receipt = task['_tool_approval_receipts'][call_id]
    if receipt.get('fileRecoveryEvidence') != check.fingerprint:
        return False
    if consume:
        receipt.pop('fileRecoveryEvidence', None)
    return True
