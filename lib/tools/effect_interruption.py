"""Close lost-worker file calls with an interruption observation, never success.

Entry: record_interrupted_file_effects, called inside the existing Turn retirement
transaction before settlement. The stored occurrence remains the only evidence
owner. Native storage implements the same contract in turn_effect_interruption.rs;
selected-authority tests compare their persisted results and resume behavior.
"""
from collections import Counter
from collections.abc import Mapping
import json


def _hex(value, size):
    return isinstance(value, str) and len(value) == size and all(c in '0123456789abcdef' for c in value)


def record_interrupted_file_effects(projection, *, conversation_id, turn_id, attempt_id, task_id):
    """Mutate only exact current-attempt occurrences with no captured tool result.

    This observation closes the model protocol pair, not the invocation or file
    operation. No filesystem query, retry, approval, worker or new journal runs.
    """
    if not all(isinstance(v, str) and v for v in (conversation_id, turn_id, attempt_id, task_id)):
        return 0
    rounds = projection.get('toolRounds')
    if not isinstance(rounds, list):
        return 0
    counts = Counter(row['executionEffect'].get('operationId') for row in rounds
                     if isinstance(row, Mapping) and isinstance(row.get('executionEffect'), Mapping)
                     and isinstance(row['executionEffect'].get('operationId'), str))
    changed = 0
    for row in rounds:
        if (not isinstance(row, dict) or row.get('toolName') != 'write_file'
                or row.get('taskId') != task_id or row.get('attemptId') != attempt_id
                or row.get('toolContent') is not None or row.get('toolModelContent') not in (None, [])
                or row.get('_providerAttemptDiscarded') is True):
            continue
        effect = row.get('executionEffect')
        if (not isinstance(effect, dict) or effect.get('scope') != 'attempt'
                or effect.get('recovery') != 'reconcile' or effect.get('state') not in ('entered', 'unknown')):
            continue
        operation = effect.get('operationId')
        reference, publication = effect.get('inspection'), effect.get('reconciliation')
        if (not _hex(operation, 32) or counts[operation] != 1
                or not isinstance(reference, Mapping) or not isinstance(publication, Mapping)
                or reference.get('operationId') != operation
                or reference.get('conversationId') != conversation_id or reference.get('turnId') != turn_id
                or not _hex(reference.get('ownerScope'), 24)
                or publication.get('kind') != 'project_file_write' or publication.get('taskId') != task_id):
            continue
        from lib.tools.result_envelope import typed_tool_error
        arguments = json.dumps({'operation_id': operation, 'turn_id': turn_id}, ensure_ascii=False,
                               sort_keys=True, separators=(',', ':'))
        action = 'Call inspect_tool_effect(' + arguments + '). Do not repeat the write automatically.'
        from lib.tools.result_envelope import tool_error_next_action
        if tool_error_next_action(action) != action:
            # A clipped identity would make the advertised query uncallable.
            continue
        row['toolContent'] = typed_tool_error('tool_effect_unknown', retryable=False,
            message=('The producing executor was retired before a tool response was captured. '
                     'File publication remains unknown. This is an interruption observation, not '
                     'a handler response, publication proof, task completion or approval.'),
            next_action=action).to_model_text()
        row['status'] = 'aborted'
        row['executionEffect'] = {**effect, 'state': 'unknown'}
        changed += 1
    return changed
