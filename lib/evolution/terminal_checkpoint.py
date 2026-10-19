"""Owned task-result commit closes the gap before terminal archive handoff.

Preparation writes immutable source parts only. The ordinary task-result CAS
commits their frozen manifest; the existing run consumer materializes it after
checking ownership, dispatch identity and every source digest. No replay.
"""
from copy import deepcopy
import json

from lib.turn_verdict import terminal_finish_reason
from .contracts import EvolutionStateError
from . import stage_outcomes as outcomes


def persistence_preparer(run, *, user_id, review_prompt_factory):
    """Retain only this stage's immutable identity and review-input context."""
    context = deepcopy({key: run[key] for key in (
        'run_id', 'task_id', 'episode_id', 'attempt', 'status', 'stage_launch', 'stage_dispatch')}
        | ({'project_path':run['project_path']} if run.get('project_path') else {})
        | ({'require_command_execution':True} if run.get('require_command_execution') else {})
        | ({'require_requirement_originals':True} if run.get('require_requirement_originals') else {})
        | ({'validation_preregistration_target':run['validation_preregistration_target']} if run.get('validation_preregistration_target') else {})
        | ({'scientific_protocol':run['scientific_protocol']} if run.get('scientific_protocol') else {})
        | ({'scientific_required_outcome':run['scientific_required_outcome']} if run.get('scientific_required_outcome') else {}))

    background_evidence = None
    parts_prepared = False

    def prepare(task, content, content_epoch):
        nonlocal background_evidence, parts_prepared
        frozen_task = {**task, 'content': content}
        candidate = task.get('_terminalOutcomeCandidate')
        if candidate is None:
            value = outcomes.prepare_metadata(context, frozen_task, user_id=user_id,
                review_prompt_factory=lambda text: review_prompt_factory(text, frozen_task))
            background_evidence = frozen_task.get('_evolutionBackgroundEvidence')
            candidate = {'schema_version': 1, 'owner_user_id': user_id,
                         'content_epoch': content_epoch, 'dispatch': context['stage_dispatch'],
                         'outcome': {**value, 'archive_state': 'sealed', 'revision': 2}}
            outcomes._validate(candidate, 'terminal_checkpoint')
            task['_terminalOutcomeCandidate'] = candidate
        if (candidate['content_epoch'] != content_epoch
                or candidate['outcome']['status'] != task.get('status')):
            raise EvolutionStateError('terminal checkpoint input changed')
        if parts_prepared:
            value = candidate['outcome']
            outcomes._verify_content(value, user_id, prefix_chars=0)
            for source in ('tool_evidence', 'artifact_evidence'):
                if value.get(source):
                    outcomes._verify_content(value, user_id, prefix_chars=0, source_kind=source)
        else:
            if background_evidence is not None:
                frozen_task['_evolutionBackgroundEvidence'] = background_evidence
            outcomes.prepare_parts(candidate['outcome'], frozen_task, user_id=user_id)
            parts_prepared = True
            background_evidence = None
        return candidate

    return prepare


def materialize_committed(run, *, user_id):
    """Use a terminal capsule only from its authenticated owned result row."""
    from lib.storage import get_storage_client
    row = get_storage_client().query('task_results.replay_get',
        {'key': run['task_id'], 'user_id': user_id, 'include_metadata': True}, deadline=0.5)
    if not row:
        return None
    meta = row.get('metadata') or {}
    if isinstance(meta, str):
        try:
            meta = json.loads(meta)
        except (ValueError, TypeError) as exc:
            raise EvolutionStateError('terminal result metadata is invalid') from exc
    if not isinstance(meta, dict):
        raise EvolutionStateError('terminal result metadata is invalid')
    capsule = meta.get('terminalContext')
    if capsule is None:
        return None
    outcomes._validate(capsule, 'terminal_checkpoint')
    value = capsule['outcome']
    outcomes._validate(value, 'stage_outcome')
    outcomes._check_binding(run, value)
    dispatch = run.get('stage_dispatch') or {}
    if (capsule['owner_user_id'] != user_id or row.get('user_id') != user_id
            or row.get('task_id') != run['task_id'] or row.get('conv_id') != ''
            or dispatch != capsule['dispatch'] or dispatch.get('state') not in ('admitted', 'refused')
            or dispatch.get('task_id') != run['task_id']
            or type(row.get('version')) is not int or row['version'] <= dispatch['birth_version']
            or row.get('status') != value['status']
            or type(meta.get('contentEpoch')) is not int
            or meta['contentEpoch'] != capsule['content_epoch']
            or terminal_finish_reason({'finishReason': meta.get('finishReason'), 'status': row.get('status')}) != value['finish_reason']
            or value['archive_state'] != 'sealed'):
        raise EvolutionStateError('terminal checkpoint owned commit binding changed')
    # Sources are non-authoritative until this complete verification succeeds.
    try:
        outcomes._verify_content(value, user_id, prefix_chars=0)
        if value.get('tool_evidence'):
            outcomes._verify_content(value, user_id, prefix_chars=0, source_kind='tool_evidence')
        if value.get('artifact_evidence'):
            outcomes._verify_content(value, user_id, prefix_chars=0, source_kind='artifact_evidence')
    except outcomes.OutcomePending as exc:
        # The authenticated row still proves consumption, even if an original is missing.
        exc.committed_outcome = value
        raise
    outcomes._put_once(outcomes.NAMESPACE, outcomes._key(value['task_id'], user_id),
                       value, 'stage outcome')
    return value


def require_prepared_delivery(task):
    """An early observer cannot bypass the owned result preparation boundary."""
    if task.get('_terminalOutcomeCandidate') is None:
        raise EvolutionStateError('terminal persistence preparation has not completed')
    return task
