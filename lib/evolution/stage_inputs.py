"""Immutable birth input and replaceable working contexts; runs own dispatch.

Logical model references are portable; routing resources and execution sessions
are never serialized. Known inline credential fields are refused. Missing,
non-JSON or changed input fails closed.
"""
import base64
import hashlib
import json
import uuid

from . import records
from .contracts import EvolutionStateError
from .runs import _validate

NAMESPACE = 'evolution.stage_inputs.v1'
# Inline/multipart crossover sizes storage frames, never input admission.
_INLINE_INPUT_BYTES = 1 << 20
INPUT_PARTS_NAMESPACE = 'evolution.stage_input_parts.v1'
MODEL_ATTEMPTS_NAMESPACE = 'evolution.model_attempts.v1'
CHILD_LIFECYCLES_NAMESPACE = 'evolution.child_lifecycles.v1'
TOOL_ATTEMPTS_NAMESPACE = 'evolution.tool_attempts.v1'
_INPUT_PART_BYTES = 256 * 1024


def canonical(value):
    try:
        encoded = json.dumps(value, ensure_ascii=False, sort_keys=True,
                             separators=(',', ':'), allow_nan=False).encode('utf-8')
    except (TypeError, ValueError) as exc:
        raise EvolutionStateError('prepared stage input is not portable JSON') from exc
    return encoded


def _no_credentials(value):
    if isinstance(value, dict):
        for key, item in value.items():
            if key.lower().replace('_', '') in {'apikey', 'accesstoken', 'authorization', 'providerkey', 'clientsecret'}:
                raise EvolutionStateError('prepared stage configuration contains inline credentials')
            _no_credentials(item)
    elif isinstance(value, list):
        for item in value:
            _no_credentials(item)


def canonical_configuration(value):
    """Validate complete portable settings without archiving inline secrets."""
    _no_credentials(value)
    return canonical(value)


def _binding(run, stage, *, project=True):
    return {'run_id':run['run_id'],'goal_id':run['goal_id'],'plan_id':run['plan_id'],'stage':stage,
            **({'project_path':run['project_path']} if project and stage=='executing' and run.get('project_path') else {})}


def freeze_input(run, task, stage, *, user_id):
    value = {'schema_version': 1, 'run_id': run['run_id'],
             'intent_id': run['stage_launch']['intent_id'], 'task_id': task['id'],
             'user_id': user_id, 'stage': stage, 'created_at': task['created_at'],
             'messages': task['messages'], 'config': task['config'],
             'attended': task['_attended']}
    _validate(value, 'prepared_input')
    binding = value['config'].get('evolution') or {}
    if (type(value['config'].get('userId')) is not int
            or value['config']['userId'] != user_id
            or binding != _binding(run,stage)):
        raise EvolutionStateError('prepared stage configuration binding changed')
    from .stage_launch import read_launch, validate_task_configuration
    from .runs import _stage_execution_limits
    intent = read_launch(run['stage_launch'], run_id=run['run_id'], user_id=user_id)
    validate_task_configuration(intent, value['config'],
                                current_limits=_stage_execution_limits(run, stage))
    if (isinstance(value['config'].get('modelRef'), dict)
            and value['config'].get('model') != task.get('model')):
        raise EvolutionStateError('prepared model differs from the task route binding')
    canonical_configuration(value['config'])
    encoded = canonical(value)
    digest = hashlib.sha256(encoded).hexdigest()
    reference = {'task_id': task['id'], 'sha256': digest}
    key = records.entity_key(task['id'], user_id, kind='prepared stage')
    existing = records.get_record(NAMESPACE, key)
    if existing is not None:
        if canonical(_decode_input(existing, user_id=user_id)) != encoded:
            raise EvolutionStateError('prepared stage archive differs from the pending carrier')
    else:
        archived = value
        if len(encoded) > _INLINE_INPUT_BYTES:
            count = (len(encoded) + _INPUT_PART_BYTES - 1) // _INPUT_PART_BYTES
            archived = {'schema_version': 2, 'task_id': task['id'], 'user_id': user_id,
                        'sha256': digest, 'byte_count': len(encoded), 'part_count': count}
            _validate(archived, 'prepared_input_manifest')
            for index in range(count):
                chunk = encoded[index*_INPUT_PART_BYTES:(index+1)*_INPUT_PART_BYTES]
                part = {'schema_version': 1, 'index': index,
                        'sha256': hashlib.sha256(chunk).hexdigest(),
                        'data': base64.b64encode(chunk).decode('ascii')}
                _validate(part, 'prepared_input_part')
                _put_immutable(INPUT_PARTS_NAMESPACE,
                               _input_part_key(task['id'], digest, index, user_id), part)
        _put_immutable(NAMESPACE, key, archived)
    # Acknowledged archive precedes the run's dispatch binding. Cancellation of
    # this restorable carrier must also acknowledge its durable abort signal.
    task['_preparedInputDigest'] = digest
    return reference


def _put_immutable(namespace, key, value, *, kind='prepared stage'):
    """A retry may acknowledge the same bytes, never replace another archive."""
    from lib.storage.errors import StorageError
    try:
        records.put_record(namespace, key, value, kind=kind, expected_version=0)
    except StorageError as exc:
        if exc.code != 'database_conflict':
            raise
        if records.get_record(namespace, key) != value:
            raise EvolutionStateError(f'{kind} immutable archive differs') from exc


def _input_part_key(task_id, digest, index, user_id):
    # Hash the identity so even maximum-length task IDs fit the record key bound.
    identity = hashlib.sha256(task_id.encode('utf-8')).hexdigest()
    return records.entity_key(f'{identity}-{digest}-{index}', user_id, kind='prepared stage part')


def _decode_input(archived, *, user_id):
    if not isinstance(archived, dict) or archived.get('schema_version') != 2:
        return archived
    _validate(archived, 'prepared_input_manifest')
    if archived['user_id'] != user_id:
        raise EvolutionStateError('prepared stage owner changed')
    count = (archived['byte_count'] + _INPUT_PART_BYTES - 1) // _INPUT_PART_BYTES
    if archived['part_count'] != count:
        raise EvolutionStateError('prepared stage framing changed')
    encoded = bytearray()
    body_digest = hashlib.sha256()
    for index in range(count):
        part = records.get_record(INPUT_PARTS_NAMESPACE,
            _input_part_key(archived['task_id'], archived['sha256'], index, user_id))
        _validate(part, 'prepared_input_part')
        try:
            chunk = base64.b64decode(part['data'], validate=True)
        except (ValueError, TypeError) as exc:
            raise EvolutionStateError('prepared stage frame encoding changed') from exc
        expected = min(_INPUT_PART_BYTES, archived['byte_count'] - index*_INPUT_PART_BYTES)
        if (part['index'] != index or len(chunk) != expected
                or hashlib.sha256(chunk).hexdigest() != part['sha256']):
            raise EvolutionStateError('prepared stage frame changed')
        body_digest.update(chunk)
        encoded.extend(chunk)
    if body_digest.hexdigest() != archived['sha256']:
        raise EvolutionStateError('prepared stage body digest changed')
    try:
        value = json.loads(encoded)
    except (ValueError, UnicodeError) as exc:
        raise EvolutionStateError('prepared stage body is not portable JSON') from exc
    if (not isinstance(value, dict) or value.get('task_id') != archived['task_id']
            or value.get('user_id') != user_id):
        raise EvolutionStateError('prepared stage manifest identity changed')
    return value


def read_input(run, *, user_id):
    reference = (run.get('stage_dispatch') or {}).get('prepared_input')
    _validate(reference, 'prepared_input_ref')
    key = records.entity_key(reference['task_id'], user_id, kind='prepared stage')
    archived = records.get_record(NAMESPACE, key)
    if isinstance(archived, dict) and archived.get('schema_version') == 2:
        if archived.get('task_id') != reference['task_id'] or archived.get('sha256') != reference['sha256']:
            raise EvolutionStateError('prepared stage manifest binding changed')
    value = _decode_input(archived, user_id=user_id)
    _validate(value, 'prepared_input')
    binding = value['config'].get('evolution') or {}
    if (hashlib.sha256(canonical(value)).hexdigest() != reference['sha256']
            or value['task_id'] != reference['task_id'] or value['user_id'] != user_id
            or value['run_id'] != run['run_id'] or value['stage'] != run['status']
            or value['intent_id'] != run['stage_dispatch']['intent_id']
            or type(value['config'].get('userId')) is not int
            or value['config'].get('userId') != user_id
            or binding != _binding(run,run['status'],project='project_path' in binding)):
        raise EvolutionStateError('prepared stage identity or digest changed')
    from .stage_launch import read_launch, validate_task_configuration
    intent = read_launch(run['stage_launch'], run_id=run['run_id'], user_id=user_id)
    if 'task_config' in intent:
        validate_task_configuration(intent, value['config'],
                                    current_limits=intent['task_config']['executionLimits'])
    canonical_configuration(value['config'])
    return value


# Working contexts are reconstructible transport, unlike immutable birth inputs.
# Two reusable banks bound growth by context size rather than number of rounds.
WORKING_NAMESPACE = 'evolution.stage_checkpoints.v1'
WORKING_PARTS_NAMESPACE = 'evolution.stage_checkpoint_parts.v1'
_WORKING_PART_BYTES = 256 * 1024  # Framing below record limits; no context-size cap.


def _working_key(task_id, user_id, suffix=''):
    return records.entity_key(task_id+suffix,user_id,kind='working checkpoint')


def install_working_checkpoint_sink(task):
    """Reuse the shared root's write-ahead/settlement port; never launch a task."""
    if task.get('_evolutionWorkingCheckpointSinkInstalled'):
        return
    import threading
    previous=task.get('_invocationCheckpointSink')
    lock=threading.Lock()
    def publish(carrier,messages,round_num):
        if carrier is not task:
            raise EvolutionStateError('working checkpoint carrier changed')
        if not lock.acquire(blocking=False):
            raise EvolutionStateError('concurrent working checkpoints are not authorized')
        try:
            save_working_checkpoint(carrier,messages,round_num)
            if callable(previous):previous(carrier,messages,round_num)
        finally:
            lock.release()
    install_model_attempt_observer(task)
    task['_evolutionWorkingCheckpointPreviousSink']=previous
    task['_invocationCheckpointSink']=publish
    task['_evolutionWorkingCheckpointSinkInstalled']=True


def _model_attempt_key(task_id, ordinal, phase, user_id):
    identity = hashlib.sha256(task_id.encode('utf-8')).hexdigest()
    suffix = 'dispatch' if phase == 'dispatch_intent' else 'terminal'
    return records.entity_key(f'{identity}-{ordinal}-{suffix}',user_id,kind='model attempt')


def install_model_attempt_observer(task):
    """Journal send intents before transport; settlement never blocks teardown."""
    binding = {'schema_version':1, **dict(task['_evolutionCheckpointBinding'])}
    _validate(binding,'model_attempt_binding')
    if binding['task_id'] != task['id'] or binding['user_id'] != task['_userId']:
        raise EvolutionStateError('model attempt binding changed')
    def observe(event):
        if event['root_task_id'] != binding['task_id']:
            raise EvolutionStateError('model attempt account changed')
        value = {'schema_version':1,'binding':binding,
                 **{key:item for key,item in event.items() if key!='root_task_id'}}
        _validate(value,'model_attempt_event')
        try:
            _put_immutable(MODEL_ATTEMPTS_NAMESPACE,
                _model_attempt_key(binding['task_id'],event['attempt_ordinal'],event['phase'],binding['user_id']),value,kind='model attempt')
        except Exception:
            from lib.log import get_logger
            get_logger(__name__).warning('Evolution model attempt observation was not acknowledged',exc_info=True)
            raise
    def observe_child(event):
        if event['root_task_id'] != binding['task_id']:
            raise EvolutionStateError('child account changed')
        value={'schema_version':1,'binding':binding,
               **{key:item for key,item in event.items() if key!='root_task_id'}}
        _validate(value,'child_lifecycle_event')
        identity=hashlib.sha256(binding['task_id'].encode('utf-8')).hexdigest()
        suffix='attach' if event['phase']=='attach_intent' else 'terminal'
        key=records.entity_key(f'{identity}-{event["child_ordinal"]}-{suffix}',binding['user_id'],kind='child lifecycle')
        try:_put_immutable(CHILD_LIFECYCLES_NAMESPACE,key,value,kind='child lifecycle')
        except Exception:
            from lib.log import get_logger
            get_logger(__name__).warning('Evolution child observation was not acknowledged',exc_info=True)
            raise
    def observe_tool(event):
        if event['root_task_id']!=binding['task_id']:
            raise EvolutionStateError('tool account changed')
        value={'schema_version':1,'binding':binding,
               **{key:item for key,item in event.items() if key!='root_task_id'}}
        _validate(value,'tool_attempt_event')
        identity=hashlib.sha256(binding['task_id'].encode('utf-8')).hexdigest()
        suffix='intent' if event['phase']=='intent' else 'terminal'
        key=records.entity_key(f'{identity}-{event["tool_ordinal"]}-{suffix}',binding['user_id'],kind='tool attempt')
        try:_put_immutable(TOOL_ATTEMPTS_NAMESPACE,key,value,kind='tool attempt')
        except Exception:
            from lib.log import get_logger
            get_logger(__name__).warning('Evolution tool observation was not acknowledged',exc_info=True)
            raise
    budget = task.get('_executionBudget')
    if budget is not None:
        budget.bind_model_attempt_observer(observe,child_observer=observe_child,tool_observer=observe_tool)
    task['_executionModelAttemptObserver'] = observe
    task['_executionChildObserver'] = observe_child
    task['_executionToolObserver'] = observe_tool


def read_tool_attempt_event(run, ordinal, phase, *, user_id):
    """Read owner-bound executor evidence; a return grants no effect verdict."""
    if type(ordinal) is not int or ordinal<1 or phase not in ('intent','terminal'):
        raise EvolutionStateError('invalid tool attempt reference')
    identity=hashlib.sha256(run['task_id'].encode('utf-8')).hexdigest()
    key=records.entity_key(f'{identity}-{ordinal}-{phase}',user_id,kind='tool attempt')
    value=records.get_record(TOOL_ATTEMPTS_NAMESPACE,key)
    if value is None:return None
    _validate(value,'tool_attempt_event')
    marker=run.get('stage_dispatch') or {}
    expected={'schema_version':1,'user_id':user_id,
        **{key:run[key] for key in ('run_id','task_id','goal_id','plan_id')},
        'stage':(run.get('stage_launch') or {}).get('stage'),
        'intent_id':marker.get('intent_id'),'birth_version':marker.get('birth_version')}
    if (value['binding']!=expected or value['tool_ordinal']!=ordinal
            or (value['phase']=='intent')!=(phase=='intent')):
        raise EvolutionStateError('tool attempt identity changed')
    return value


def read_child_lifecycle_event(run, ordinal, phase, *, user_id):
    """Read frozen membership evidence; detached never means effects completed."""
    if type(ordinal) is not int or ordinal<1 or phase not in ('attach_intent','terminal'):
        raise EvolutionStateError('invalid child lifecycle reference')
    identity=hashlib.sha256(run['task_id'].encode('utf-8')).hexdigest()
    suffix='attach' if phase=='attach_intent' else 'terminal'
    key=records.entity_key(f'{identity}-{ordinal}-{suffix}',user_id,kind='child lifecycle')
    value=records.get_record(CHILD_LIFECYCLES_NAMESPACE,key)
    if value is None:return None
    _validate(value,'child_lifecycle_event')
    marker=run.get('stage_dispatch') or {}
    expected={'schema_version':1,'user_id':user_id,
        **{key:run[key] for key in ('run_id','task_id','goal_id','plan_id')},
        'stage':(run.get('stage_launch') or {}).get('stage'),
        'intent_id':marker.get('intent_id'),'birth_version':marker.get('birth_version')}
    if (value['binding']!=expected or value['child_ordinal']!=ordinal
            or (value['phase']=='attach_intent')!=(phase=='attach_intent')):
        raise EvolutionStateError('child lifecycle identity changed')
    return value


def read_model_attempt_event(run, ordinal, phase, *, user_id):
    """Read one owned immutable observation; never dispatch or infer a verdict."""
    if type(ordinal) is not int or ordinal < 1 or phase not in ('dispatch_intent','terminal'):
        raise EvolutionStateError('invalid model attempt reference')
    key = _model_attempt_key(run['task_id'],ordinal,phase,user_id)
    value = records.get_record(MODEL_ATTEMPTS_NAMESPACE,key)
    if value is None:
        return None
    _validate(value,'model_attempt_event')
    marker = run.get('stage_dispatch') or {}
    expected = {'schema_version':1,'user_id':user_id,
        **{key:run[key] for key in ('run_id','task_id','goal_id','plan_id')},
        'stage':(run.get('stage_launch') or {}).get('stage'),
        'intent_id':marker.get('intent_id'),'birth_version':marker.get('birth_version')}
    if (value['binding'] != expected or value['attempt_ordinal'] != ordinal
            or (value['phase']=='dispatch_intent') != (phase=='dispatch_intent')):
        raise EvolutionStateError('model attempt identity changed')
    return value


def read_model_attempt_token_lower_bound(run, *, user_id):
    """Stream this task's immutable observations; no total/completeness inference."""
    marker = run.get('stage_dispatch') or {}
    expected = {'schema_version':1,'user_id':user_id,
        **{key:run[key] for key in ('run_id','task_id','goal_id','plan_id')},
        'stage':(run.get('stage_launch') or {}).get('stage'),
        'intent_id':marker.get('intent_id'),'birth_version':marker.get('birth_version')}
    _validate(expected,'model_attempt_binding')
    prefix = hashlib.sha256(run['task_id'].encode('utf-8')).hexdigest()+'-'
    cursor='';count=0;tokens=0;last_ordinal=0;account_tokens=None;page_size=records.MAX_LIST_LIMIT
    while True:
        rows=records.list_record_page(MODEL_ATTEMPTS_NAMESPACE,user_id,kind='model attempt',
                                     limit=page_size,after_key=cursor,entity_prefix=prefix)
        for row in rows:
            value=row['value'];_validate(value,'model_attempt_event')
            if (value['binding'] != expected or row['key'] != _model_attempt_key(
                    run['task_id'],value['attempt_ordinal'],value['phase'],user_id)):
                raise EvolutionStateError('model attempt identity changed')
            if value['attempt_ordinal'] != last_ordinal:
                count+=1;last_ordinal=value['attempt_ordinal']
            if value['phase'] != 'dispatch_intent':
                tokens+=value['reported_tokens']
            if 'account_observed_tokens' in value:
                account_tokens=max(account_tokens or 0,value['account_observed_tokens'])
        if len(rows)<page_size:
            break
        cursor=rows[-1]['key']
    return ({'count':count,'tokens':tokens,'stage':expected['stage'],
             **({'account_tokens':account_tokens} if account_tokens is not None else {})} if count else None)


def read_tool_account_lower_bound(run, *, user_id):
    """Read charged slots in task-indexed pages; never infer effect settlement."""
    marker=run.get('stage_dispatch') or {}
    expected={'schema_version':1,'user_id':user_id,
        **{key:run[key] for key in ('run_id','task_id','goal_id','plan_id')},
        'stage':(run.get('stage_launch') or {}).get('stage'),
        'intent_id':marker.get('intent_id'),'birth_version':marker.get('birth_version')}
    _validate(expected,'model_attempt_binding')
    identity=hashlib.sha256(run['task_id'].encode('utf-8')).hexdigest()
    cursor='';count=0;last_ordinal=0;calls=None;page_size=records.MAX_LIST_LIMIT
    while True:
        rows=records.list_record_page(TOOL_ATTEMPTS_NAMESPACE,user_id,kind='tool attempt',
            limit=page_size,after_key=cursor,entity_prefix=identity+'-')
        for row in rows:
            value=row['value'];_validate(value,'tool_attempt_event')
            suffix='intent' if value['phase']=='intent' else 'terminal'
            key=records.entity_key(f'{identity}-{value["tool_ordinal"]}-{suffix}',user_id,kind='tool attempt')
            if value['binding']!=expected or row['key']!=key:
                raise EvolutionStateError('tool attempt identity changed')
            if value['tool_ordinal']!=last_ordinal:
                count+=1;last_ordinal=value['tool_ordinal']
            if 'account_tool_calls' in value:calls=max(calls or 0,value['account_tool_calls'])
        if len(rows)<page_size:break
        cursor=rows[-1]['key']
    return {'count':count,'calls':calls,'stage':expected['stage']} if count and calls is not None else None


def save_working_checkpoint(task,messages,round_num):
    """Publish all parts before one acknowledged pointer; failed intent stops effects."""
    import base64
    from .bridge import execution_checkpoint
    from lib.goal_runs.contract import GOAL_WORKER_STATE_KEYS
    from lib.log import get_logger
    binding=execution_checkpoint(task)
    if binding is None:
        raise EvolutionStateError('working checkpoint lacks server-owned stage binding')
    body={'messages':messages,'task_state':{key:task[key] for key in GOAL_WORKER_STATE_KEYS if key in task},'usage':task.get('usage') or {}}
    from lib.tasks_pkg.background_commands import task_effect_snapshot
    budget=task.get('_executionBudget')
    if budget is not None and budget.root_id!=task['id']:
        raise EvolutionStateError('working checkpoint execution account belongs to another root')
    body['effects']={'account':budget.continuity_snapshot() if budget is not None else None,
        'background':task_effect_snapshot(task_id=task['id'],user_id=binding['user_id'])}
    _validate(body,'working_context')
    try:
        encoded=json.dumps(body,ensure_ascii=False,separators=(',',':'),allow_nan=False).encode('utf-8')
    except (ValueError,TypeError) as exc:
        raise EvolutionStateError('working checkpoint is not portable JSON') from exc
    uid=binding['user_id'];task_id=binding['task_id'];key=_working_key(task_id,uid)
    previous,version=records.get_versioned_record(WORKING_NAMESPACE,key)
    if previous is not None:
        _validate(previous,'working_checkpoint')
        if any(previous['binding'][field]!=binding[field] for field in binding if field!='actuals'):
            raise EvolutionStateError('working checkpoint stage binding changed')
        if previous['input_sha256']!=task.get('_preparedInputDigest'):
            raise EvolutionStateError('working checkpoint immutable input changed')
    digest=hashlib.sha256(encoded).hexdigest()
    published=task.get('_evolutionWorkingCheckpointPublished') or {}
    reuse=bool(previous and published.get('version')==version and published.get('sha256')==digest
        and previous['sha256']==digest and previous['byte_count']==len(encoded)
        and previous['input_sha256']==task.get('_preparedInputDigest'))
    bank=previous['bank'] if reuse else (1-previous['bank'] if previous else 0)
    count=(len(encoded)+_WORKING_PART_BYTES-1)//_WORKING_PART_BYTES
    header={'schema_version':1,'binding':binding,'input_sha256':task.get('_preparedInputDigest'),
            'phase':task.get('_invocationCheckpointPhase'),'round':round_num,'bank':bank,
            'part_count':count,'byte_count':len(encoded),'sha256':digest}
    _validate(header,'working_checkpoint')
    if previous:
        from .contracts import load_schema
        from .runs import SCHEMA
        order={row['const']:row['x-progress-order'] for row in load_schema(SCHEMA)['$defs']['working_checkpoint']['properties']['phase']['oneOf']}
        if (header['round']<previous['round']
                or (header['round']==previous['round'] and order[header['phase']]<order[previous['phase']])
                or (previous['phase']=='terminal_delivery' and header['phase']!='terminal_delivery')):
            raise EvolutionStateError('working checkpoint progress regressed; publication refused')
    bank_key=_working_key(task_id,uid,f'-bank-{bank}')
    manifest,bank_version=records.get_versioned_record(WORKING_PARTS_NAMESPACE,bank_key)
    if manifest is not None:_validate(manifest,'working_checkpoint_bank')
    highwater=max(count,(manifest or {}).get('highwater',0))
    # Reserve addresses before writing parts, retaining cleanup knowledge even
    # if publication fails halfway through a larger working set.
    if not reuse:
        caches=task.setdefault('_evolutionWorkingCheckpointFrames',{})
        cached=caches.get(bank) or {}
        hashes=dict(cached.get('hashes') or {}) if cached.get('version')==bank_version else {}
        records.put_record(WORKING_PARTS_NAMESPACE,bank_key,{'highwater':highwater},kind='working bank',expected_version=bank_version)
        bank_version+=1
        caches[bank]={'version':bank_version,'hashes':hashes}
        for index in range(count):
            data=encoded[index*_WORKING_PART_BYTES:(index+1)*_WORKING_PART_BYTES]
            frame_digest=hashlib.sha256(data).hexdigest()
            if hashes.get(index)==frame_digest:continue
            # An ambiguous write never leaves a remembered old hash: a later
            # publication must rewrite this address before it can skip it.
            hashes.pop(index,None)
            part={'schema_version':2,'sha256':frame_digest,'index':index,'data':base64.b64encode(data).decode('ascii')}
            records.put_record(WORKING_PARTS_NAMESPACE,_working_key(task_id,uid,f'-bank-{bank}-part-{index}'),part,kind='working part')
            hashes[index]=frame_digest
    committed_version=records.put_record(WORKING_NAMESPACE,key,header,kind='working checkpoint',expected_version=version)
    task['_evolutionWorkingCheckpointPublished']={'sha256':digest,'version':committed_version}
    # Only unused addresses in the published bank are reclaimed. The other
    # bank and every immutable input/outcome/observation namespace stay intact.
    try:
        if highwater!=count and header['phase'] in ('ready','terminal_delivery'):
            task.get('_evolutionWorkingCheckpointFrames',{}).pop(bank,None)
            _delete_working_rows(task_id,uid,bank=bank,keep=count)
            records.put_record(WORKING_PARTS_NAMESPACE,bank_key,{'highwater':count},kind='working bank',expected_version=bank_version)
        if header['phase'] in ('ready','terminal_delivery'):
            inactive=1-bank
            inactive_key=_working_key(task_id,uid,f'-bank-{inactive}')
            inactive_manifest,inactive_version=records.get_versioned_record(WORKING_PARTS_NAMESPACE,inactive_key)
            if inactive_manifest is not None:
                _validate(inactive_manifest,'working_checkpoint_bank')
                if inactive_manifest['highwater']>count:
                    task.get('_evolutionWorkingCheckpointFrames',{}).pop(inactive,None)
                    _delete_working_rows(task_id,uid,bank=inactive,keep=count)
                    records.put_record(WORKING_PARTS_NAMESPACE,inactive_key,{'highwater':count},kind='working bank',expected_version=inactive_version)
    except Exception as exc:
        get_logger(__name__).warning('Working checkpoint cleanup deferred for %s: %s',task_id,exc)
    return header


def read_working_checkpoint_metadata(run,*,user_id):
    """Owner-bound context metadata; consumption remains an incomplete lower bound."""
    key=_working_key(run['task_id'],user_id)
    header,version=records.get_versioned_record(WORKING_NAMESPACE,key)
    if header is None:return None
    _validate(header,'working_checkpoint')
    expected={field:run[field] for field in ('run_id','task_id','goal_id','plan_id')}
    expected.update(user_id=user_id,stage=(run.get('stage_launch') or {}).get('stage'),
        intent_id=(run.get('stage_dispatch') or {}).get('intent_id'),birth_version=(run.get('stage_dispatch') or {}).get('birth_version'))
    if (any(header['binding'].get(field)!=value for field,value in expected.items())
            or header['input_sha256']!=(run.get('stage_dispatch') or {}).get('prepared_input',{}).get('sha256')
            or header['part_count']!=(header['byte_count']+_WORKING_PART_BYTES-1)//_WORKING_PART_BYTES):
        raise EvolutionStateError('working checkpoint binding or framing changed')
    if version<1:raise EvolutionStateError('working checkpoint has no acknowledged version')
    return {'checkpoint':header,'version':version}


def read_working_checkpoint(run,*,user_id):
    """Verify one complete current prefix; this read grants no recovery admission."""
    import base64
    observed=read_working_checkpoint_metadata(run,user_id=user_id)
    if observed is None:return None
    header=observed['checkpoint'];version=observed['version']
    key=_working_key(run['task_id'],user_id)
    read_input({**run,'status':header['binding']['stage']},user_id=user_id)
    pieces=[]
    for index in range(header['part_count']):
        part=records.get_record(WORKING_PARTS_NAMESPACE,_working_key(run['task_id'],user_id,f"-bank-{header['bank']}-part-{index}"))
        _validate(part,'working_checkpoint_part')
        if part['index']!=index or (part.get('schema_version')!=2 and part['sha256']!=header['sha256']):
            raise EvolutionStateError('working checkpoint part identity changed')
        try:data=base64.b64decode(part['data'],validate=True)
        except ValueError as exc:raise EvolutionStateError('working checkpoint encoding changed') from exc
        if part.get('schema_version')==2 and hashlib.sha256(data).hexdigest()!=part['sha256']:
            raise EvolutionStateError('working checkpoint frame digest changed')
        if len(data)!=min(_WORKING_PART_BYTES,header['byte_count']-index*_WORKING_PART_BYTES):
            raise EvolutionStateError('working checkpoint part size changed')
        pieces.append(data)
    encoded=b''.join(pieces)
    if hashlib.sha256(encoded).hexdigest()!=header['sha256']:
        raise EvolutionStateError('working checkpoint digest changed')
    try:body=json.loads(encoded)
    except (ValueError,UnicodeError) as exc:raise EvolutionStateError('working checkpoint JSON changed') from exc
    _validate(body,'working_context')
    current,current_version=records.get_versioned_record(WORKING_NAMESPACE,key)
    if current_version!=version or current!=header:
        raise EvolutionStateError('working checkpoint advanced during read')
    return {'checkpoint':header,'context':body,'version':version}


def _delete_working_record(namespace, key):
    """Confirm reconstructible transport deletion, retrying only typed transients."""
    from lib.storage.errors import StorageError
    client = records._storage(write=True)
    command_id = 'evolution-working-delete:' + uuid.uuid4().hex
    def absent():
        row = client.query('record.get', {'namespace': namespace, 'key': key})
        return row is None or (isinstance(row, dict) and 'value' in row and row['value'] is None)
    for attempt in range(len(records._COMMIT_RETRY_DELAYS_S) + 1):
        try:
            result = client.command('record.delete', {'namespace': namespace, 'key': key}, command_id)
            if not isinstance(result, dict) or type(result.get('deleted')) is not bool:
                raise EvolutionStateError('working checkpoint cleanup was not acknowledged')
            if not absent():
                raise EvolutionStateError('working checkpoint cleanup was not exactly acknowledged')
            return
        except StorageError as exc:
            # A committed delete with a lost response is complete only when its
            # selected-authority address is absent; never infer that from timeout.
            try:
                if absent():
                    return
            except StorageError:
                pass
            if not exc.retryable or attempt >= len(records._COMMIT_RETRY_DELAYS_S):
                raise
            records._sleep(max(records._COMMIT_RETRY_DELAYS_S[attempt],
                               (exc.retry_after_ms or 0) / 1000.0))


def _delete_working_rows(task_id,user_id,*,bank=None,keep=0):
    prefix=_working_key(task_id,user_id,f'-bank-{bank}-part-' if bank is not None else '-bank-')
    cursor=''
    while True:
        rows=records._read_with_retry('record.list',{'namespace':WORKING_PARTS_NAMESPACE,'prefix':prefix,
            'after_key':cursor,'limit':8},kind='working checkpoint cleanup')
        if not isinstance(rows,list):raise EvolutionStateError('invalid working checkpoint cleanup page')
        if not rows:return
        for row in rows:
            key=row.get('key')
            if not isinstance(key,str) or not key.startswith(prefix) or key<=cursor:
                raise EvolutionStateError('working checkpoint cleanup escaped its owner range')
            cursor=key
            if bank is not None:
                try:index=int(key.rsplit('-part-',1)[1])
                except (ValueError,IndexError):raise EvolutionStateError('invalid working checkpoint part address')
                if index<keep:continue
            _delete_working_record(WORKING_PARTS_NAMESPACE,key)


def release_working_checkpoint(task_id,*,user_id):
    """Reclaim transport after verified terminal capture; never delete originals."""
    from lib.log import get_logger
    try:
        key=_working_key(task_id,user_id)
        header=records.get_record(WORKING_NAMESPACE,key)
        if header is None:return
        _validate(header,'working_checkpoint')
        if header['binding']['user_id']!=user_id or header['binding']['task_id']!=task_id:
            raise EvolutionStateError('working checkpoint cleanup binding changed')
        _delete_working_rows(task_id,user_id)
        _delete_working_record(WORKING_NAMESPACE,key)
    except Exception as exc:
        get_logger(__name__).warning('Working checkpoint release deferred for %s: %s',task_id,exc)
