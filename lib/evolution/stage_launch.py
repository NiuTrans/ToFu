"""Immutable stage requests for restart-safe evolution handoffs.

The run record's CAS pointer authorizes dispatch. This archive owns only frozen
model-visible inputs; task creation, execution, settlement and cancellation
remain in runs.py and the shared task engine. Explicit owner identity scopes
every request, including reads after process-local task evidence is released.
"""
from __future__ import annotations

import hashlib
import time
from copy import deepcopy

from lib.storage.errors import StorageError
from . import records
from .contracts import EvolutionContractError, EvolutionStateError, contract_validator

NAMESPACE = 'evolution.stage_launches.v1'
SCHEMA = 'evolution_run_v1.schema.json'


def _validate(value, definition):
    import jsonschema
    try:
        contract_validator(SCHEMA, definition).validate(value)
    except jsonschema.ValidationError as exc:
        raise EvolutionContractError(exc.message[:300]) from exc
    return value


def read_launch(reference, *, run_id, user_id):
    """Read and validate the immutable request named by a committed run pointer."""
    _validate(reference, 'stage_launch_ref')
    key = records.entity_key(reference['intent_id'], user_id, kind='stage launch')
    value = records.get_record(NAMESPACE, key)
    if value is None:
        raise EvolutionStateError('stage launch evidence unavailable; dispatch was refused')
    _validate(value, 'stage_launch')
    if value['run_id'] != run_id or value['stage'] != reference['stage']:
        raise EvolutionStateError('stage launch does not match the owned run')
    if ('task_config_sha256' in reference and reference['task_config_sha256']
            != value.get('task_config_sha256')):
        raise EvolutionStateError('frozen stage configuration witness changed')
    if 'task_config' in value:
        _check_configuration(value)
    return value


def _sealed_reference(reference, value):
    return {**reference, **({'task_config_sha256': value['task_config_sha256']}
                           if 'task_config' in value else {})}


def _check_configuration(value):
    from .stage_inputs import canonical_configuration
    config = value['task_config']
    binding = config['evolution']
    if (binding['run_id'] != value['run_id'] or binding['stage'] != value['stage']
            or hashlib.sha256(canonical_configuration(config)).hexdigest()
            != value['task_config_sha256']):
        raise EvolutionStateError('frozen stage configuration identity or digest changed')


def _configured_value(value, configuration):
    from .stage_inputs import canonical_configuration
    config = deepcopy(configuration)
    value['task_config'] = config
    value['task_config_sha256'] = hashlib.sha256(canonical_configuration(config)).hexdigest()
    _validate(value, 'stage_launch')
    _check_configuration(value)
    return value


def validate_task_configuration(intent, configuration, *, current_limits):
    """Refuse post-hook expansion before any stage submission is admitted."""
    if 'task_config' not in intent:
        return
    from .contracts import load_schema
    _validate(configuration, 'stage_task_config')
    frozen = intent['task_config']
    fields = load_schema(SCHEMA)['$defs']['stage_task_config']['x-frozenPermissionFields']
    for key in fields:
        # A structured reference is the route authority; the task engine
        # resolves provider offerings to a logical model label at allocation.
        if key == 'model' and isinstance(frozen.get('modelRef'), dict):
            continue
        if key in frozen and (key not in configuration or configuration[key] != frozen[key]):
            raise EvolutionStateError('prepared stage permissions differ from the frozen launch')
    for key, original in frozen['executionLimits'].items():
        current = current_limits[key]
        ceiling = current if original is None else original if current is None else min(original, current)
        actual = configuration['executionLimits'][key]
        if ceiling is not None and (actual is None or actual > ceiling):
            raise EvolutionStateError('prepared stage limits exceed the frozen launch')


def freeze_launch(run, stage, *, user_id, prompt_factory, task_config_factory=None):
    """Commit source evidence before the run can authorize its next stage.

    Deterministic request identity reuses the first acknowledged snapshot after
    ambiguous archive writes. No prompt is regenerated once a request exists.
    """
    identity = f'{run["run_id"]}:{stage}:{run["attempt"]}:{len(run["receipts"])}'
    reference = {'intent_id': 'stage-' + hashlib.sha256(identity.encode()).hexdigest()[:32],
                 'stage': stage}
    current = run.get('stage_launch')
    if current and current['stage'] == stage:
        configured = read_launch(current, run_id=run['run_id'], user_id=user_id)
        if configured.get('launch_epoch_id') == reference['intent_id']:
            return deepcopy(current)
    key = records.entity_key(reference['intent_id'], user_id, kind='stage launch')
    existing = records.get_record(NAMESPACE, key)
    if existing is not None:
        recorded = read_launch(reference, run_id=run['run_id'], user_id=user_id)
        return _sealed_reference(reference, recorded)
    value = {'schema_version': 1, 'run_id': run['run_id'], 'stage': stage,
             'source_task_id': run['task_id'], 'prompt': prompt_factory(),
             'created_at': int(time.time())}
    if task_config_factory is not None:
        value['launch_epoch_id'] = reference['intent_id']
        _configured_value(value, task_config_factory())
    _validate(value, 'stage_launch')
    try:
        records.put_record(NAMESPACE, key, value, kind='stage launch', expected_version=0)
    except StorageError as exc:
        if exc.code != 'database_conflict':
            raise
        # Concurrent preparers share the first committed input, but only one
        # wins the run CAS that authorizes task dispatch.
        recorded = read_launch(reference, run_id=run['run_id'], user_id=user_id)
        return _sealed_reference(reference, recorded)
    return _sealed_reference(reference, value)


def supersede_model_launch(run, request, *, user_id):
    """Prepare a linked request for an explicit owner change before allocation.

    The run CAS still grants dispatch. Other settings, original prompt and
    limits remain frozen; ambiguous ACKs reuse the same prepared successor.
    Already allocated stages and legacy prompt-only intents keep their policy.
    """
    previous = run.get('stage_launch')
    if run['status'] != 'preparing' or not previous:
        return previous
    stage = {'executor': 'executing', 'reviewer': 'reviewing'}[request['role']]
    if previous['stage'] != stage:
        return previous
    original = read_launch(previous, run_id=run['run_id'], user_id=user_id)
    if 'task_config' not in original:
        return previous
    from .role_models import selected_model
    chosen = selected_model(run, stage)
    config = deepcopy(original['task_config'])
    config.pop('model', None)
    config.pop('modelRef', None)
    if chosen:
        config.update(modelRef=dict(chosen), model=chosen.get('model_id') or chosen.get('offering_id'))
    value = {**deepcopy(original), 'created_at': int(time.time()),
             'supersedes': deepcopy(previous), 'model_change_request': deepcopy(request)}
    _configured_value(value, config)
    from .stage_inputs import canonical_configuration
    identity = previous['intent_id'] + ':' + hashlib.sha256(canonical_configuration(
        {'config_sha256': value['task_config_sha256'], 'request': request})).hexdigest()
    reference = {'intent_id': 'stage-' + hashlib.sha256(identity.encode()).hexdigest()[:32], 'stage': stage}
    key = records.entity_key(reference['intent_id'], user_id, kind='stage launch')
    existing = records.get_record(NAMESPACE, key)
    if existing is not None:
        recorded = read_launch(reference, run_id=run['run_id'], user_id=user_id)
        if (recorded.get('supersedes') != previous or recorded.get('model_change_request') != request
                or recorded['prompt'] != original['prompt']
                or recorded['task_config_sha256'] != value['task_config_sha256']):
            raise EvolutionStateError('model successor does not match its prepared authority')
        return _sealed_reference(reference, recorded)
    try:
        records.put_record(NAMESPACE, key, value, kind='stage launch', expected_version=0)
    except StorageError as exc:
        if exc.code != 'database_conflict':
            raise
        recorded = read_launch(reference, run_id=run['run_id'], user_id=user_id)
        if (recorded.get('supersedes') != previous or recorded.get('model_change_request') != request
                or recorded['prompt'] != original['prompt']
                or recorded['task_config_sha256'] != value['task_config_sha256']):
            raise EvolutionStateError('model successor authority changed') from exc
        return _sealed_reference(reference, recorded)
    return _sealed_reference(reference, value)
