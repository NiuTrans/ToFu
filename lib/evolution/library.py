"""Owner- and run-scoped material shelf. No fetching, crawling or resident cache.

Entry points: list_materials, save_material, remove_material and material tool.
One CAS record per goal-run, <=64 links; payload schema lives in evolution_run_v1.
Metadata is a collected reference, never a verification or quality receipt.
"""
from __future__ import annotations
import hashlib
import json
import time
from urllib.parse import urlsplit, urlunsplit
from . import records
from .contracts import EvolutionContractError, EvolutionStateError, contract_validator, load_schema
from lib.storage.errors import StorageError

SCHEMA = 'evolution_run_v1.schema.json'
MAX_MATERIALS = 64
MAX_LIBRARY_BYTES = 192 * 1024


def _validate(value, definition):
    import jsonschema
    try:
        contract_validator(SCHEMA, definition).validate(value)
    except jsonschema.ValidationError as exc:
        raise EvolutionContractError(exc.message[:300]) from exc
    return value


def _owner_run(run_id, user_id):
    from .runs import get_run
    run = get_run(run_id, user_id=user_id)
    if not run:
        raise EvolutionStateError('run not found for this owner')
    return run


def _assert_owner_run_exists(run_id,user_id):
    """Ownership-only reads do not project or authorize receipt history."""
    key=records.entity_key(run_id,user_id,kind='run')
    header=records.get_record(records.NAMESPACE_RUNS,key)
    if not header or header.get('run_id')!=run_id:
        raise EvolutionStateError('run not found for this owner')


def _key(run_id, user_id):
    return records.entity_key(run_id, user_id, kind='library')


def list_materials(run_id, *, user_id):
    _owner_run(run_id, user_id)
    row = records.get_record(records.NAMESPACE_LIBRARIES, _key(run_id, user_id))
    return (row or {}).get('materials', [])


def safe_material_url(value):
    if any(ord(c) <= 32 or ord(c) == 127 for c in value) or '\\' in value:
        raise EvolutionContractError('material URL must not contain whitespace or control characters')
    try:
        url = urlsplit(value)
        if url.scheme not in ('http','https') or not url.hostname or url.username or url.password:
            raise ValueError('invalid link')
        _ = url.port
    except ValueError as exc:
        raise EvolutionContractError('material URL must be an HTTP(S) link without credentials') from exc
    return urlunsplit((url.scheme, url.netloc.lower(), url.path or '/', url.query, url.fragment))


def _mutate(run_id, user_id, change, *, row_change=None):
    key = _key(run_id, user_id)
    for attempt in range(3):
        row, version = records.get_versioned_record(records.NAMESPACE_LIBRARIES, key)
        row = row or {'run_id':run_id,'materials':[]}
        result = change(row['materials'])
        if row_change is not None:result=row_change(row)
        _validate(row, 'library')
        if len(json.dumps(row['materials'], ensure_ascii=False).encode('utf-8')) > MAX_LIBRARY_BYTES:
            raise EvolutionStateError('shelf storage budget reached; shorten notes or remove a material')
        from .coordination import MAX_WORKSPACE_BYTES
        if len(json.dumps(row,ensure_ascii=False).encode('utf-8'))>MAX_WORKSPACE_BYTES:
            raise EvolutionStateError('goal workspace storage budget reached; history was not deleted')
        try:
            records.put_record(records.NAMESPACE_LIBRARIES,key,row,kind='library',expected_version=version)
            return result
        except StorageError as exc:
            if exc.code != 'database_conflict' or attempt == 2:
                raise


def save_material(run_id, payload, *, user_id, task_id='', origin=None):
    run = _owner_run(run_id, user_id)
    if task_id and (run['status'] != 'executing' or run['task_id'] != task_id):
        from .coordination import snapshot
        manager = snapshot(run_id, user_id=user_id)['manager']
        if not (manager['enabled'] and manager['status']=='running' and manager['task_id']==task_id):
            raise EvolutionStateError('material write requires a current bound task')
    _validate(payload, 'material_request')
    title = payload['title'].strip()
    if not title:
        raise EvolutionContractError('material title cannot be blank')
    url = safe_material_url(payload['url'])
    identity = urlsplit(url)._replace(fragment='').geturl()
    material_id = 'mat-' + hashlib.sha256(identity.encode()).hexdigest()[:24]
    def change(items):
        old = next((item for item in items if item['material_id']==material_id),None)
        if old is None and len(items)>=MAX_MATERIALS:
            raise EvolutionStateError('shelf is full (64 materials); remove a material before adding another')
        now = int(time.time())
        material = dict(payload, title=title, url=url, note=payload.get('note','').strip(),
                        material_id=material_id,run_id=run_id,origin=origin or ('agent' if task_id else 'user'),
                        task_id=task_id,created_at=old['created_at'] if old else now,updated_at=now)
        if old is not None:items[items.index(old)]=material
        else:items.append(material)
        return material
    from .coordination import append_to_row
    import uuid
    event_id='material:'+uuid.uuid4().hex
    def with_event(row):
        item=next(item for item in row['materials'] if item['material_id']==material_id)
        append_to_row(row,event_id=event_id,kind='material_saved',content=item['title']+'\n'+item['note'],references=[item['url']],origin=item['origin'],task_id=task_id)
        return item
    result=_mutate(run_id,user_id,change,row_change=with_event)
    from .coordinator import notify
    notify(run_id,user_id=user_id)
    return result


def remove_material(run_id, material_id, *, user_id):
    _owner_run(run_id,user_id)
    def change(items):
        items[:]=[item for item in items if item['material_id']!=material_id]
        return {'removed':material_id}
    from .coordination import append_to_row
    import uuid
    event_id='material:'+uuid.uuid4().hex
    def with_event(row):
        append_to_row(row,event_id=event_id,kind='material_removed',content=material_id,origin='user')
        return {'removed':material_id}
    result=_mutate(run_id,user_id,change,row_change=with_event)
    from .coordinator import notify
    notify(run_id,user_id=user_id)
    return result


def material_tool_definition():
    return {'type':'function','function':{
        'name':'evolution_material',
        'description':'Read or save materials for the CURRENT objective bookshelf. action=list reads 8 entries from cursor (default 0); action=save requires kind, title and url. Read the shelf to use sources added by the user. Save a useful paper, repository, web reference or dataset. Save sources as you collect them, with a concrete relevance note. Use real discovered URLs; saving a link is not a claim that it was verified. Repeated URLs update the same item. Never save secrets or credential-bearing links.',
        'parameters':load_schema(SCHEMA)['$defs']['material_tool_request']}}


def handle_material(task, tc, fn_name, tc_id, fn_args, rn, round_entry,
                    cfg, project_path, project_enabled, all_tools=None):
    from .tool_handler import _entry
    binding=(task.get('config') or {}).get('evolution') or {}
    owner=task.get('_userId')
    try:
        from .coordination import authorize_task
        run, role, _state = authorize_task(task)
        if role not in ('executing', 'coordinator'):
            raise EvolutionStateError('shelf writes require a bound executor or coordinator')
        task_id=task['id']
        args=dict(fn_args or {})
        _validate(args,'material_tool_request')
        action=args.pop('action','save')
        cursor=args.pop('cursor',0)
        if action=='list':
            items=list_materials(binding['run_id'],user_id=owner)
            page=items[cursor:cursor+8]
            output={'materials':page,'total':len(items),'next_cursor':cursor+8 if cursor+8<len(items) else None}
            return [_entry(fn_name,'Current goal materials',json.dumps(output,ensure_ascii=False))]
        item=save_material(binding['run_id'],args,user_id=owner,task_id=task_id)
    except (EvolutionContractError,EvolutionStateError,ValueError,TypeError) as exc:
        return [_entry(fn_name,'Material not saved',str(exc)[:300],fetched=False)]
    return [_entry(fn_name,item['title'],f"Saved to current goal shelf: {item['url']} · {item['material_id']}")]
