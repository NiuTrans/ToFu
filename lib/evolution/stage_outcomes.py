"""Committed terminal deliveries outside run headers.

Entry points: capture, recover, page, read_metadata. The existing run consumer
owns progression; record CAS owns immutable terminal input and chunk sealing.
No executor replay, model invocation, timer, or storage dialect lives here.
"""
from __future__ import annotations

import hashlib
import json
import time

import jsonschema
from lib.storage.errors import StorageError
from lib.turn_verdict import terminal_finish_reason
from . import records
from .bridge import terminal_actuals
from .tool_evidence import PART_CHARS
from .contracts import EvolutionContractError, EvolutionStateError, contract_validator

NAMESPACE = 'evolution.stage_outcomes.v1'
PART_NAMESPACE = 'evolution.stage_outcome_parts.v1'
BACKGROUND_NAMESPACE = 'evolution.stage_background_joins.v1'
PAGE_CHARS = 6000
SCHEMA = 'evolution_run_v1.schema.json'


class OutcomePending(EvolutionStateError):
    """Terminal metadata committed, but some output chunks are unavailable."""


def _validate(value, definition):
    try:
        contract_validator(SCHEMA, definition).validate(value)
    except jsonschema.ValidationError as exc:
        raise EvolutionContractError(exc.message[:300]) from exc
    if value.get('scientific_audit'):
        audit=value['scientific_audit']
        if audit['observation_count']!=audit['failure_count']+sum(audit['outcomes'].values()):
            raise EvolutionStateError('scientific observation aggregate is inconsistent')
    if definition == 'stage_outcome':
        if (value['revision'] != (2 if value['archive_state'] == 'sealed' else 1)
                or value['content_parts'] != (value['content_chars'] + PART_CHARS - 1) // PART_CHARS
                or (value['stage'] == 'reviewing' and value['review_prompt'] is not None)):
            raise EvolutionStateError('stage outcome metadata is inconsistent')
    if definition == 'stage_outcome' and value.get('tool_evidence'):
        evidence = value['tool_evidence']
        if (evidence['content_parts'] != (evidence['content_chars'] + PART_CHARS - 1) // PART_CHARS
                or (evidence['message_count'] == 0) != (evidence['content_chars'] == 0)):
            raise EvolutionStateError('tool evidence metadata is inconsistent')
    if definition == 'background_join':
        evidence=value['background_evidence']
        if (evidence['content_parts']!=(evidence['content_chars']+PART_CHARS-1)//PART_CHARS
                or evidence['message_count']<1 or evidence['content_chars']<1):
            raise EvolutionStateError('background evidence metadata is inconsistent')
    if definition == 'stage_outcome' and value.get('pending_background_commands'):
        commands=value['pending_background_commands']
        if value['stage']!='executing' or len({row['command_id'] for row in commands})!=len(commands):
            raise EvolutionStateError('pending background manifest is inconsistent')
    if definition == 'stage_outcome' and value.get('artifact_evidence'):
        evidence = value['artifact_evidence']
        if evidence['content_parts'] != (evidence['content_chars']+PART_CHARS-1)//PART_CHARS:
            raise EvolutionStateError('artifact evidence metadata is inconsistent')
    return value


def _key(task_id, user_id):
    return records.entity_key(task_id, user_id, kind='stage outcome')


def _part_key(task_id, ordinal, user_id, source_kind='deliverable'):
    identity = hashlib.sha256((f'{task_id}:{ordinal}' if source_kind == 'deliverable'
                              else f'{task_id}:{source_kind}:{ordinal}').encode()).hexdigest()
    return records.entity_key(identity, user_id, kind='stage outcome part')


def _digest(text):
    return hashlib.sha256(text.encode('utf-8')).hexdigest()


def _put_once(namespace, key, value, kind):
    try:
        return records.put_record(namespace, key, value, kind=kind, expected_version=0)
    except StorageError as exc:
        if exc.code != 'database_conflict':
            raise
        existing, version = records.get_versioned_record(namespace, key)
        if existing != value:
            raise EvolutionStateError(f'{kind} immutable input changed') from exc
        return version


def read_metadata(run_id, task_id, *, user_id):
    value = records.get_record(NAMESPACE, _key(task_id, user_id))
    if value is None:
        return None
    _validate(value, 'stage_outcome')
    if value['run_id'] != run_id or value['task_id'] != task_id:
        raise EvolutionStateError('stage outcome does not match the owned run')
    joined = read_background_join(value, user_id=user_id)
    if joined:
        value = {**value, 'background_evidence':joined['background_evidence'],
                 'background_command_execution_observed':joined['command_execution_observed'],
                 **({'background_scientific_audit':joined['scientific_audit']} if joined.get('scientific_audit') else {})}
    return value


def _check_binding(run, value):
    if value['stage'] == 'executing' and run.get('require_requirement_originals') != value.get('require_requirement_originals'):
        raise EvolutionStateError('required original policy binding changed')
    if value['stage']=='executing' and run.get('validation_preregistration_target')!=value.get('validation_preregistration_target'):
        raise EvolutionStateError('validation preregistration binding changed')
    if value['stage']=='executing' and (run.get('scientific_protocol')!=value.get('scientific_protocol')
            or run.get('scientific_required_outcome')!=value.get('scientific_required_outcome')):
        raise EvolutionStateError('scientific protocol binding changed')
    if (value['run_id'] != run['run_id'] or value['task_id'] != run['task_id']
            or value['episode_id'] != run['episode_id']
            or value['attempt'] != run['attempt']
            or value['intent_id'] != (run.get('stage_launch') or {}).get('intent_id', '')
            or (run['status'] in ('executing', 'reviewing') and value['stage'] != run['status'])):
        raise EvolutionStateError('stage outcome launch binding changed')


def source_view(value, source_kind='deliverable'):
    """Select one immutable source without conflating its hash or offsets."""
    if source_kind == 'deliverable':
        return value
    if source_kind not in ('tool_evidence', 'artifact_evidence', 'background_evidence') or not value.get(source_kind):
        raise EvolutionStateError('saved evidence is unavailable for this source')
    return {**value, **value[source_kind]}


def _read_part(value, ordinal, user_id, source_kind='deliverable'):
    value = source_view(value, source_kind)
    part = records.get_record(PART_NAMESPACE, _part_key(value['task_id'], ordinal, user_id, source_kind))
    if part is None:
        raise OutcomePending('terminal output archive is incomplete')
    _validate(part, 'stage_outcome_part')
    expected_length = min(PART_CHARS, value['content_chars'] - ordinal * PART_CHARS)
    if (part.get('source_kind', 'deliverable') != source_kind
            or part['run_id'] != value['run_id'] or part['task_id'] != value['task_id']
            or part['ordinal'] != ordinal or part['offset'] != ordinal * PART_CHARS
            or part['source_sha256'] != value['content_sha256']
            or len(part['text']) != expected_length
            or part['part_sha256'] != _digest(part['text'])):
        raise EvolutionStateError('terminal output chunk binding or digest changed')
    return part['text']


def _verify_content(value, user_id, *, prefix_chars=24001, source_kind='deliverable'):
    descriptor = source_view(value, source_kind)
    digest = hashlib.sha256()
    prefix = []
    retained = 0
    for ordinal in range(descriptor['content_parts']):
        text = _read_part(value, ordinal, user_id, source_kind)
        digest.update(text.encode('utf-8'))
        if retained < prefix_chars:
            clipped = text[:prefix_chars - retained]
            prefix.append(clipped)
            retained += len(clipped)
    if digest.hexdigest() != descriptor['content_sha256']:
        raise EvolutionStateError('terminal output archive digest changed')
    return ''.join(prefix)


def _seal(value, version, user_id):
    if value['archive_state'] == 'sealed':
        return value
    sealed = {**value, 'archive_state': 'sealed', 'revision': 2}
    _validate(sealed, 'stage_outcome')
    try:
        records.put_record(NAMESPACE, _key(value['task_id'], user_id), sealed,
                           kind='stage outcome', expected_version=version)
    except StorageError as exc:
        if exc.code != 'database_conflict':
            raise
        if read_metadata(value['run_id'], value['task_id'], user_id=user_id) != sealed:
            raise EvolutionStateError('terminal archive seal changed') from exc
    return sealed


def prepare_metadata(run, task, *, user_id, review_prompt_factory):
    """Build frozen terminal input without granting archive authority."""
    from .tool_evidence import capture_background_state, background_execution_observed, command_execution_observed
    config = task.get('config') or {}
    binding = config.get('evolution') or {}
    if (task.get('_userId') != user_id or task.get('id') != run['task_id']
            or binding.get('run_id') != run['run_id']
            or binding.get('stage') not in ('executing', 'reviewing')):
        raise EvolutionStateError('terminal outcome does not match the owned task')
    pending_background = []
    if binding['stage'] == 'executing':
        completed_background, pending_background = capture_background_state(task, user_id=user_id)
        task['_evolutionBackgroundEvidence'] = completed_background
        task['_evolutionPendingBackgroundEvidence'] = bool(pending_background)
        task['_evolutionBackgroundLaunchCallIds']=tuple(row['source_tool_call_id'] for row in completed_background+pending_background)
    from . import artifact_evidence
    artifact_manifest = artifact_evidence.freeze(run, task, user_id=user_id)
    artifact_descriptor = artifact_evidence.describe(artifact_manifest, user_id)
    if artifact_descriptor:task['_evolutionArtifactDescriptor'] = artifact_descriptor
    preregistered=None
    if binding['stage']=='executing' and run.get('validation_preregistration_target'):
        from .programs import validation_preregistered
        preregistered=validation_preregistered(run['validation_preregistration_target'],run['run_id'],user_id=user_id)
    scientific=None
    if binding['stage']=='executing' and run.get('scientific_protocol'):
        from .tool_evidence import scientific_audit
        scientific=scientific_audit(task,run['scientific_protocol'])
    content = str(task.get('content') or '')
    from .tool_evidence import describe
    from lib.turn_verdict import TurnStatus, derive_task_verdict
    reviewable = (binding['stage'] == 'executing' and task.get('status') == 'done'
                  and derive_task_verdict(task).status is TurnStatus.COMPLETED
                  and not task.get('error') and not task.get('_executionLimitReason')
                  and bool(content.strip()))
    digest = hashlib.sha256()
    for offset in range(0, len(content), PART_CHARS):
        digest.update(content[offset:offset + PART_CHARS].encode('utf-8'))
    actuals, complete = terminal_actuals(task)
    value = {'schema_version': 1, 'revision': 1, 'archive_state': 'collecting',
             'run_id': run['run_id'], 'task_id': task['id'],
             'episode_id': run['episode_id'], 'attempt': run['attempt'],
             'intent_id': (run.get('stage_launch') or {}).get('intent_id', ''),
             'stage': binding['stage'], 'status': task['status'],
             'finish_reason': terminal_finish_reason(task),
             'terminal_verdict': derive_task_verdict(task).status.value,
             'stream_state': str(task.get('streamState') or ''),
             'error': str(task.get('error') or '')[:500],
             'execution_limit_reason': str(task.get('_executionLimitReason') or '')[:160],
             'actuals': actuals, 'actuals_complete': complete,
             'content_chars': len(content), 'content_parts': (len(content) + PART_CHARS - 1) // PART_CHARS,
             'content_sha256': digest.hexdigest(),
             'tool_evidence': describe(task),
             **({'pending_background_commands':pending_background}
                if binding['stage']=='executing' and run.get('require_command_execution') and pending_background else {}),
             **({'command_execution_observed': (command_execution_observed(task) or background_execution_observed(task.get('_evolutionBackgroundEvidence') or []))}
                if binding['stage'] == 'executing' and run.get('require_command_execution') else {}),
             **({'artifact_evidence':artifact_descriptor} if artifact_descriptor else {}),
             **({'validation_preregistration_target':run['validation_preregistration_target'],'validation_preregistration_observed':preregistered} if preregistered is not None else {}),
             **({'scientific_protocol':run['scientific_protocol'],'scientific_audit':scientific} if scientific is not None else {}),
             **({'require_requirement_originals':True} if run.get('require_requirement_originals') else {}),
             **({'scientific_required_outcome':run['scientific_required_outcome']} if scientific is not None and run.get('scientific_required_outcome') else {}),
             'review_prompt': review_prompt_factory(content) if reviewable else None,
             'created_at': int(time.time()),
             **({'historical_source_coverage':task['_evolutionHistoricalSourceCoverage']}
                if binding['stage']=='reviewing' and task.get('_evolutionHistoricalSourceCoverage') else {}),
             **({'source_coverage': task['_evolutionSourceCoverage']} if binding['stage'] == 'reviewing' and task.get('_evolutionSourceCoverage') else {}),
             **({'tool_evidence_coverage': task['_evolutionToolEvidenceCoverage']} if binding['stage'] == 'reviewing' and task.get('_evolutionToolEvidenceCoverage') else {}),
             **({'background_evidence_coverage':task['_evolutionBackgroundEvidenceCoverage']}
                if binding['stage']=='reviewing' and task.get('_evolutionBackgroundEvidenceCoverage') else {}),
             **({'artifact_evidence_coverage':task['_evolutionArtifactCoverage']}
                if binding['stage']=='reviewing' and task.get('_evolutionArtifactCoverage') else {}),
             **({'model': config['modelRef']} if config.get('modelRef') else {})}
    _validate(value, 'stage_outcome')
    _check_binding(run, value)
    return value


def prepare_parts(value, task, *, user_id):
    """Acknowledge immutable originals; parts alone carry no verdict authority."""
    content = str(task.get('content') or '')
    if value['content_chars'] != len(content) or value['content_sha256'] != _digest(content):
        raise EvolutionStateError('terminal output immutable input changed')
    for ordinal in range(value['content_parts']):
        offset = ordinal * PART_CHARS
        text = content[offset:offset + PART_CHARS]
        part = {'schema_version': 1, 'run_id': value['run_id'], 'task_id': value['task_id'],
                'ordinal': ordinal, 'offset': offset, 'source_sha256': value['content_sha256'],
                'part_sha256': _digest(text), 'text': text}
        _validate(part, 'stage_outcome_part')
        _put_once(PART_NAMESPACE, _part_key(task['id'], ordinal, user_id), part, 'stage outcome part')
    _verify_content(value, user_id, prefix_chars=0)
    if value.get('tool_evidence'):
        from .tool_evidence import chunks, describe
        if value['tool_evidence'] != describe(task):
            raise EvolutionStateError('terminal tool evidence immutable input changed')
        descriptor = value['tool_evidence']
        for ordinal, text in enumerate(chunks(task)):
            part = {'schema_version': 1, 'run_id': value['run_id'], 'task_id': value['task_id'],
                    'source_kind': 'tool_evidence', 'ordinal': ordinal, 'offset': ordinal * PART_CHARS,
                    'source_sha256': descriptor['content_sha256'],
                    'part_sha256': _digest(text), 'text': text}
            _validate(part, 'stage_outcome_part')
            _put_once(PART_NAMESPACE, _part_key(task['id'], ordinal, user_id, 'tool_evidence'),
                      part, 'stage tool evidence part')
        _verify_content(value, user_id, prefix_chars=0, source_kind='tool_evidence')


    if value.get('artifact_evidence'):
        from . import artifact_evidence
        manifest = artifact_evidence.manifest_for(task, user_id)
        descriptor = value['artifact_evidence']
        if descriptor != artifact_evidence.describe(manifest, user_id):
            raise EvolutionStateError('terminal artifact evidence immutable input changed')
        for ordinal, text in enumerate(artifact_evidence.chunks(manifest, user_id)):
            part = {'schema_version':1,'run_id':value['run_id'],'task_id':value['task_id'],
                'source_kind':'artifact_evidence','ordinal':ordinal,'offset':ordinal*PART_CHARS,
                'source_sha256':descriptor['content_sha256'],'part_sha256':_digest(text),'text':text}
            _validate(part, 'stage_outcome_part')
            _put_once(PART_NAMESPACE, _part_key(task['id'],ordinal,user_id,'artifact_evidence'),
                      part, 'stage artifact evidence part')
        _verify_content(value, user_id, prefix_chars=0, source_kind='artifact_evidence')


def capture(run, task, *, user_id, review_prompt_factory):
    """Freeze terminal facts and exact review evidence before the run receipt CAS."""
    config = task.get('config') or {}
    binding = config.get('evolution') or {}
    if (int(task.get('_userId') or 0) != int(user_id) or task.get('id') != run['task_id']
            or binding.get('run_id') != run['run_id']
            or binding.get('stage') not in ('executing', 'reviewing')):
        raise EvolutionStateError('terminal outcome does not match the owned task')
    if task.get('_workspaceDeliveryOwnershipRejected'):
        raise EvolutionStateError('terminal result ownership was rejected')
    if task.get('_terminalOutcomeCandidate') is not None:
        from .terminal_checkpoint import materialize_committed
        committed = materialize_committed(run, user_id=user_id)
        if committed is None:
            raise EvolutionStateError('terminal checkpoint has not committed in the owned result')
        return committed
    key = _key(task['id'], user_id)
    value, version = records.get_versioned_record(NAMESPACE, key)
    if value is None:
        value = prepare_metadata(run, task, user_id=user_id, review_prompt_factory=review_prompt_factory)
        # Concurrent terminal callbacks use the first committed terminal snapshot.
        try:
            version = records.put_record(NAMESPACE, key, value, kind='stage outcome', expected_version=0)
        except StorageError as exc:
            if exc.code != 'database_conflict':
                raise
            value, version = records.get_versioned_record(NAMESPACE, key)
    _validate(value, 'stage_outcome')
    _check_binding(run, value)
    if value['archive_state'] == 'sealed':
        task.pop('_evolutionBackgroundEvidence', None)
        return value
    prepare_parts(value, task, user_id=user_id)
    sealed = _seal(value, version, user_id)
    task.pop('_evolutionBackgroundEvidence', None)
    return sealed


def recover(run, *, user_id):
    """Restore acknowledged terminal facts; never invoke an executor."""
    value, version = records.get_versioned_record(NAMESPACE, _key(run['task_id'], user_id))
    if value is None:
        from .terminal_checkpoint import materialize_committed
        value = materialize_committed(run, user_id=user_id)
        if value is None:
            return None, None
        value, version = records.get_versioned_record(NAMESPACE, _key(run['task_id'], user_id))
    _validate(value, 'stage_outcome')
    _check_binding(run, value)
    content = _verify_content(value, user_id, prefix_chars=(value['content_chars']
                               if value['stage'] == 'reviewing' else 24001))
    if value.get('tool_evidence'):
        _verify_content(value, user_id, prefix_chars=0, source_kind='tool_evidence')
    if value.get('artifact_evidence'):
        _verify_content(value, user_id, prefix_chars=0, source_kind='artifact_evidence')
    value = _seal(value, version, user_id)
    task = {'id': value['task_id'], '_userId': user_id, 'status': value['status'],
            'content': content, 'finishReason': value['finish_reason'],
            'streamState': value['stream_state'] or None, 'error': value['error'],
            '_executionLimitReason': value['execution_limit_reason'],
            'config': {'evolution': {'run_id': value['run_id'], 'stage': value['stage']},
                       **({'modelRef': value['model']} if value.get('model') else {})}}
    if value.get('historical_source_coverage'):
        task['_evolutionHistoricalSourceCoverage'] = value['historical_source_coverage']
    if value.get('source_coverage'):
        task['_evolutionSourceCoverage'] = value['source_coverage']
    if value.get('tool_evidence_coverage'):
        task['_evolutionToolEvidenceCoverage'] = value['tool_evidence_coverage']
    if value.get('background_evidence_coverage'):
        task['_evolutionBackgroundEvidenceCoverage'] = value['background_evidence_coverage']
    if value.get('artifact_evidence_coverage'):
        task['_evolutionArtifactCoverage'] = value['artifact_evidence_coverage']
    if (value['stage'] == 'executing' and value['status'] == 'done'
            and value['terminal_verdict'] == 'completed'
            and not value['error'] and not value['execution_limit_reason']
            and content.strip()
            and value['review_prompt'] is None):
        raise EvolutionStateError('completed execution has no frozen review evidence')
    return value, task


def page(run_id, task_id, *, user_id, offset=0, source_kind='deliverable', limit=PAGE_CHARS):
    """Owner read only; full Unicode output remains available outside run headers."""
    if type(limit) is not int or not 1<=limit<=PAGE_CHARS:
        raise ValueError('outcome page limit is outside the source read bound')
    from .runs import get_run
    run = get_run(run_id, user_id=user_id)
    if run is None or (task_id != run['task_id']
                      and not any(row['task_id'] == task_id for row in run['receipts'])):
        raise EvolutionStateError('unknown outcome for this owned run')
    value = read_metadata(run_id, task_id, user_id=user_id)
    if value is None:
        raise EvolutionStateError('unknown outcome for this owned run')
    if source_kind not in ('deliverable', 'tool_evidence', 'artifact_evidence', 'background_evidence'):
        raise ValueError('unknown outcome source kind')
    descriptor = source_view(value, source_kind)
    if isinstance(offset, bool) or not isinstance(offset, int) or not 0 <= offset <= descriptor['content_chars']:
        raise ValueError('outcome offset is outside the source')
    end = min(descriptor['content_chars'], offset + limit)
    content = []
    for ordinal in range(offset // PART_CHARS, (end + PART_CHARS - 1) // PART_CHARS):
        text = _read_part(value, ordinal, user_id, source_kind)
        start = ordinal * PART_CHARS
        content.append(text[max(0, offset - start):end - start])
    return _validate({'task_id': task_id, 'stage': value['stage'], 'status': value['status'],
                      'content': ''.join(content), 'offset': offset, 'total_chars': descriptor['content_chars'],
                      'next_offset': end if end < descriptor['content_chars'] else None,
                      'source_kind': source_kind,
                      'tool_evidence_available': bool(value.get('tool_evidence', {}).get('message_count')),
                      'artifact_evidence_available':bool(value.get('artifact_evidence')),
                      'background_evidence_available':bool(value.get('background_evidence')),
                      **({'background_evidence_scope':value['background_evidence']['scope']}
                         if value.get('background_evidence') else {}),
                      **({'artifact_evidence_scope':value['artifact_evidence']['scope'],
                          'artifact_evidence_complete':value['artifact_evidence']['complete']}
                         if value.get('artifact_evidence') else {}),
                      **({'tool_evidence_scope': value['tool_evidence']['scope']} if value.get('tool_evidence') else {}),
                      'archive_state': value['archive_state'], 'terminal_verdict': value['terminal_verdict'],
                      'source_sha256': descriptor['content_sha256'],
                      'actuals': value['actuals'], 'actuals_complete': value['actuals_complete']},
                     'stage_outcome_page')


def _background_parent_digest(value):
    raw = {key:item for key,item in value.items() if key not in
           ('background_evidence','background_command_execution_observed','background_scientific_audit')}
    return _digest(json.dumps(raw,sort_keys=True,ensure_ascii=False,separators=(',',':')))


def read_background_join(parent, *, user_id):
    """An immutable late source binds the original committed terminal manifest."""
    if not parent.get('pending_background_commands'):
        return None
    joined = records.get_record(BACKGROUND_NAMESPACE,_key(parent['task_id'],user_id))
    if joined is None:
        return None
    _validate(joined,'background_join')
    if (joined['run_id']!=parent['run_id'] or joined['task_id']!=parent['task_id']
            or joined['parent_sha256']!=_background_parent_digest(parent)):
        raise EvolutionStateError('background completion source parent changed')
    return joined


def join_background_results(parent, *, user_id):
    """Return complete/running/unavailable without waiting or executing work.

    Only manifests from a sealed original are accepted. Source parts precede
    immutable join publication; the ordinary run CAS owns review admission.
    """
    if parent['archive_state']!='sealed' or not parent.get('pending_background_commands'):
        raise EvolutionStateError('background wait has no committed launch manifest')
    existing=read_background_join(parent,user_id=user_id)
    if existing:
        _verify_content({**parent,'background_evidence':existing['background_evidence']},user_id,prefix_chars=0,source_kind='background_evidence')
        return 'complete',existing
    from .tool_evidence import capture_background_state,describe,chunks,background_execution_observed
    launches=parent['pending_background_commands']
    task={'id':parent['task_id'],'_userId':user_id,'messages':[],'toolRounds':[
        {'toolName':'run_command','status':'done','toolCallId':row['source_tool_call_id'],
         'results':[{'exitCode':'background','backgroundCommandId':row['command_id']}]} for row in launches]}
    # The sealed launch set below distinguishes missing producers from running
    # ones. Initial capture retains all unresolved launches; reconciliation
    # must still reach recovery/unavailability classification for missing rows.
    completed,pending=capture_background_state(task,user_id=user_id,retain_unresolved=False)
    if pending:return 'running',None
    if {row['command_id'] for row in completed}!={row['command_id'] for row in launches}:
        from lib.tasks_pkg.background_commands import recovery_needs_retry,task_has_running_commands
        recovering = recovery_needs_retry() or task_has_running_commands(task_id=parent['task_id'],user_id=user_id)
        return ('running' if recovering else 'unavailable'),None
    task['_evolutionBackgroundEvidence']=completed
    descriptor={**describe(task),'scope':'owned_background_command_completions'}
    view={**parent,'background_evidence':descriptor}
    for ordinal,text in enumerate(chunks(task)):
        part={'schema_version':1,'run_id':parent['run_id'],'task_id':parent['task_id'],
              'source_kind':'background_evidence','ordinal':ordinal,'offset':ordinal*PART_CHARS,
              'source_sha256':descriptor['content_sha256'],'part_sha256':_digest(text),'text':text}
        _validate(part,'stage_outcome_part')
        _put_once(PART_NAMESPACE,_part_key(parent['task_id'],ordinal,user_id,'background_evidence'),part,'background evidence part')
    _verify_content(view,user_id,prefix_chars=0,source_kind='background_evidence')
    joined={'run_id':parent['run_id'],'task_id':parent['task_id'],
            'parent_sha256':_background_parent_digest(parent),'background_evidence':descriptor,
            'command_execution_observed':background_execution_observed(completed)}
    if parent.get('scientific_protocol'):
        from .tool_evidence import scientific_audit,merge_scientific_audits
        joined['scientific_audit']=merge_scientific_audits(parent['scientific_audit'],scientific_audit(task,parent['scientific_protocol']))
    _validate(joined,'background_join')
    _put_once(BACKGROUND_NAMESPACE,_key(parent['task_id'],user_id),joined,'background completion source')
    return 'complete',joined
