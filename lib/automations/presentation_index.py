"""Identity-only index for producer-bound task time outputs.

Entry points: completed_presentations, presentation_prefix, validate_target.
The run owns content. Storage authorities publish references with run progress;
readers prove the canonical owned attempt before returning its presentation.
"""
import hashlib

from .errors import AutomationError
from .registry import builtin_registry


def presentation_prefix(owner_prefix, conversation_id, attempt_id=''):
    prefix = owner_prefix + hashlib.sha256(conversation_id.encode()).hexdigest() + ':'
    return prefix + hashlib.sha256(attempt_id.encode()).hexdigest() + ':' if attempt_id else prefix


def validate_target(event, attempt):
    evidence = event['evidence']
    if (not attempt or any(attempt.get(key) != evidence.get(key) for key in
            ('attemptId', 'taskId', 'turnId', 'conversationId'))
            or type(attempt.get('startedAt')) is not int
            or attempt['startedAt'] / 1000 != evidence.get('startedAt')
            or event['occurredAt'] != evidence.get('startedAt')):
        raise AutomationError('automation_storage_integrity', 'Presentation has no matching owned physical start')


def completed_presentations(previous, run):
    """Yield newly successful producer-bound outputs; freeze prior publication."""
    event = run['trigger']
    if not event['id'].startswith('task-start:'):
        return
    if event['kind'] != 'task.started':
        raise ValueError('Presentation producer kind differs')
    for step in run['snapshot']['steps']:
        if step['action'] != 'task.show_time':
            continue
        step_id = step['id']
        current, old = run['steps'][step_id], previous['steps'][step_id]
        if old['status'] == 'succeeded':
            if current != old:
                raise ValueError('Published time output is immutable')
            continue
        if current['status'] != 'succeeded':
            continue
        evidence = event.get('evidence', {})
        for key, maximum in [('attemptId',128),('taskId',200),('turnId',256),('conversationId',256)]:
            value = evidence.get(key)
            if not isinstance(value,str) or not 0 < len(value) <= maximum:
                raise ValueError('Presentation source identity is missing')
        source_id = 'task-start:' + hashlib.sha256(
            f"{evidence['attemptId']}:{evidence['taskId']}".encode()).hexdigest()
        if event['id'] != source_id:
            raise ValueError('Presentation source identity differs')
        if old['status'] != 'dispatching' or not current.get('effectKey') or old.get('effectKey') != current['effectKey']:
            raise ValueError('Presentation requires its committed effect intent')
        output = current.get('output')
        builtin_registry().get('task.show_time').validate(output,output=True)
        if (output['startedAt'] != evidence.get('startedAt')
                or output['timezone'] != current['arguments'].get('timezone','UTC')):
            raise ValueError('Presentation output differs from frozen start evidence')
        yield {'id':hashlib.sha256(current['effectKey'].encode()).hexdigest(),
               'runId':run['id'],'stepId':step_id,'ownerId':run['ownerId'],
               'tenantId':run['tenantId'],**{key:evidence[key] for key in
                   ('conversationId','attemptId','turnId')}}
