"""Immutable model-budget policy bound to an owned acknowledged research task.

Entry: bind_model_budget/read_model_budget. Uses existing semantic records on
selected storage. This binds a caller-authorized policy, not execution ownership,
physical usage, goal admission, scientific validity, or permission to resume.
"""
from copy import deepcopy
from functools import lru_cache
import hashlib
import json
from pathlib import Path
from jsonschema import Draft202012Validator,ValidationError
from lib.identity import require_user_id
from lib.storage.errors import StorageError
from lib.agent_core.execution_limits import validate_execution_limits
from lib.evolution import records as record_store
from . import records as research_records

NAMESPACE='research.model_budget_bindings.v1'
MAX_BINDING_BYTES=4096

@lru_cache(maxsize=3)
def _validator(kind='binding'):
    schema=json.loads((Path(__file__).resolve().parents[2]/'contracts'/'research_model_budget_v1.schema.json').read_text())
    if kind in ('attempt_event','discovery_execution_owner'):
        schema = {'$schema':schema['$schema'], '$defs':schema['$defs'],
                  '$ref':f'#/$defs/{kind}'}
    elif kind != 'binding':
        raise ValueError('unknown research model budget contract')
    Draft202012Validator.check_schema(schema)
    if kind == 'discovery_execution_owner':
        from referencing import Registry,Resource
        directory=Path(__file__).resolve().parents[2]/'contracts'
        shared_path=directory/'research_action_observation_v1.schema.json'
        shared=json.loads(shared_path.read_text())
        schema['$id']=(directory/'research_model_budget_v1.schema.json').as_uri()
        registry=Registry().with_resource(shared_path.as_uri(),Resource.from_contents(shared))
        return Draft202012Validator(schema,registry=registry)
    return Draft202012Validator(schema)

def normalize_model_limits(execution_limits):
    """Validate and copy model-only policy before allocating a research task."""
    try:
        schema = _validator().schema['properties']['execution_limits']
        Draft202012Validator(schema).validate(execution_limits)
        validate_execution_limits(execution_limits)
        return deepcopy(execution_limits)
    except (ValidationError, TypeError, ValueError) as error:
        raise ValueError('invalid research model execution limits') from error


def _encode(value):
    encoded=json.dumps(value,ensure_ascii=False,sort_keys=True,separators=(',',':'),allow_nan=False).encode()
    if len(encoded)>MAX_BINDING_BYTES:raise ValueError('research model budget binding exceeds byte bound')
    return encoded

def _validate(value):
    try:
        _validator().validate(value)
        validate_execution_limits(value['execution_limits'])
        _encode(value)
    except (ValidationError,KeyError,TypeError,ValueError) as error:
        raise ValueError('invalid research model budget binding') from error

def _source(task_id,user_id):
    owner=require_user_id(user_id,context='research model budget owner')
    if not isinstance(task_id,str) or not 1<=len(task_id)<=160:
        raise ValueError('invalid research model budget task identity')
    source=research_records.load_record(task_id,user_id=owner,internal=True)
    if not isinstance(source,dict) or source.get('task_id')!=task_id:
        raise ValueError('research model budget requires an owned durable task')
    identity={key:source[key] for key in ('task_id','direction','lang','created_at','request')}
    digest=hashlib.sha256(json.dumps(identity,ensure_ascii=False,sort_keys=True,
        separators=(',',':'),allow_nan=False).encode()).hexdigest()
    key=record_store.entity_key(hashlib.sha256(task_id.encode()).hexdigest(),owner,kind='research model budget')
    return owner,source,digest,key

def read_model_budget(task_id,*,user_id):
    owner,source,digest,key=_source(task_id,user_id)
    value=record_store.get_record(NAMESPACE,key)
    if value is None:return None
    _validate(value)
    if (value['owner_user_id']!=owner or value['task_id']!=task_id
            or value['source_identity_sha256']!=digest):
        raise StorageError('database_integrity','Research model budget source identity changed')
    return deepcopy(value)

def bind_model_budget(task_id,*,user_id,execution_limits):
    owner,source,digest,key=_source(task_id,user_id)
    if source.get('status') not in research_records.ACTIVE:
        raise ValueError('terminal research task cannot receive a model budget')
    value={'schema_version':1,'owner_user_id':owner,'task_id':task_id,
           'source_identity_sha256':digest,'execution_limits':deepcopy(execution_limits)}
    _validate(value)
    expected=_encode(value)
    def exact():
        observed=record_store.get_record(NAMESPACE,key)
        if observed is None:return False
        _validate(observed)
        return _encode(observed)==expected
    existing=record_store.get_record(NAMESPACE,key)
    if existing is not None:
        if not exact():raise StorageError('database_conflict','Research model budget is immutable')
        return deepcopy(value)
    try:
        record_store.put_record(NAMESPACE,key,value,kind='research model budget',expected_version=0)
    except StorageError:
        if not exact():raise
    if not exact():
        raise StorageError('database_internal','Research model budget binding was not exactly acknowledged')
    return deepcopy(value)
